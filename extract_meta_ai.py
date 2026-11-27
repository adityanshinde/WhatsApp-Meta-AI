"""
WhatsApp Web Meta AI Media Auto-Extractor
=========================================
Automatically extracts all photos and videos from a WhatsApp Web chat (e.g. Meta AI)
without manually clicking Download on each item.

Usage:
    python extract_meta_ai.py
    python extract_meta_ai.py --mode scroll --scrolls 40
    python extract_meta_ai.py --cdp http://127.0.0.1:9222
    python extract_meta_ai.py --limit 100 --out my_media
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright

ROOT = Path(__file__).resolve().parent
DEFAULT_PROFILE = ROOT / "wa_profile"
DEFAULT_OUT = ROOT / "downloads"

MIME_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}

FETCH_BLOB_JS = """
async (blobUrl) => {
    const res = await fetch(blobUrl);
    if (!res.ok) throw new Error('fetch failed: ' + res.status);
    const blob = await res.blob();
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return {
        mime: blob.type || '',
        size: bytes.length,
        b64: btoa(binary),
    };
}
"""

FIND_VIEWER_MEDIA_JS = """
() => {
    // Look for full-screen media viewer / lightbox image or video (supports both blob: and mmg.whatsapp.net)
    const isMediaSrc = (s) => s.startsWith('blob:') || (s.startsWith('https://') && !s.includes('static.whatsapp.net'));
    const candidates = [];
    for (const v of document.querySelectorAll('video')) {
        const src = v.currentSrc || v.src || (v.querySelector('source') && v.querySelector('source').src) || '';
        const r = v.getBoundingClientRect();
        if (isMediaSrc(src) && r.width >= 200 && r.height >= 200) {
            candidates.push({ kind: 'video', src, area: r.width * r.height, w: r.width, h: r.height });
        }
    }
    for (const img of document.querySelectorAll('img')) {
        const src = img.currentSrc || img.src || '';
        const r = img.getBoundingClientRect();
        if (isMediaSrc(src) && r.width >= 220 && r.height >= 220) {
            candidates.push({ kind: 'image', src, area: r.width * r.height, w: r.width, h: r.height });
        }
    }
    candidates.sort((a, b) => b.area - a.area);
    return candidates[0] || null;
}
"""

FIND_ALL_CHAT_MEDIA_JS = """
(minSize) => {
    const out = [];
    const seen = new Set();
    const root = document.querySelector('#main') || document;
    const isMediaSrc = (s, alt) => {
        if (s.startsWith('blob:')) return true;
        if (!s.startsWith('https://')) return false;
        if (s.includes('static.whatsapp.net')) return false;
        // t61.24694-24 are 96x96 profile pictures
        if (s.includes('/t61.24694-24/')) return false;
        if (alt === 'Meta AI generated image') return true;
        return s.includes('mmg.whatsapp.net') || s.includes('cdn.whatsapp.net') || s.includes('fna.whatsapp.net');
    };
    for (const el of root.querySelectorAll('img, video')) {
        const isVideo = el.tagName === 'VIDEO';
        const src = isVideo
            ? (el.currentSrc || el.src || (el.querySelector('source') && el.querySelector('source').src) || '')
            : (el.currentSrc || el.src || '');
        const alt = el.getAttribute('alt') || '';
        if (!isMediaSrc(src, alt) || seen.has(src)) continue;
        const r = el.getBoundingClientRect();
        const nw = isVideo ? (el.videoWidth || r.width) : (el.naturalWidth || r.width);
        const nh = isVideo ? (el.videoHeight || r.height) : (el.naturalHeight || r.height);
        if (nw >= minSize && nh >= minSize) {
            seen.add(src);
            out.push({ kind: isVideo ? 'video' : 'image', src, w: nw, h: nh });
        }
    }
    return out;
}
"""


def sniff_ext(raw: bytes, mime: str, kind: str) -> str:
    # Check magic bytes first when server sends application/octet-stream or empty mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return ".mp4"

    clean_mime = mime.lower().split(";")[0].strip()
    if clean_mime and clean_mime != "application/octet-stream":
        ext = MIME_EXT_MAP.get(clean_mime) or mimetypes.guess_extension(clean_mime) or ""
        if ext and ext != ".bin":
            return ext
    return ".mp4" if kind == "video" else ".jpg"


class MediaStore:
    """Deduplicated media saver backed by downloads/manifest.json."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.out_dir / "manifest.json"
        self.hashes: dict[str, str] = {}
        self.counter = 0
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                self.hashes = data.get("hashes", {})
                self.counter = int(data.get("counter", len(self.hashes)))
            except Exception:
                pass

    def _save_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps({"counter": self.counter, "hashes": self.hashes}, indent=2),
            encoding="utf-8",
        )

    def save_bytes(self, raw: bytes, mime: str, kind: str = "image") -> tuple[bool, Path]:
        digest = hashlib.sha256(raw).hexdigest()
        if digest in self.hashes:
            existing = self.out_dir / self.hashes[digest]
            if existing.exists():
                return False, existing

        ext = sniff_ext(raw, mime, kind)

        self.counter += 1
        filename = f"meta_ai_{self.counter:04d}_{digest[:8]}{ext}"
        dest = self.out_dir / filename
        dest.write_bytes(raw)
        self.hashes[digest] = filename
        self._save_manifest()
        return True, dest


def extract_media(page: Page, url: str) -> tuple[bytes, str]:
    if url.startswith("https://") or url.startswith("http://"):
        resp = page.request.get(url, timeout=30_000)
        if resp.ok:
            return resp.body(), resp.headers.get("content-type", "")
    data: dict[str, Any] = page.evaluate(FETCH_BLOB_JS, url)
    return base64.b64decode(data["b64"]), data.get("mime", "")


def open_latest_media_in_chat(page: Page) -> bool:
    """Click the last large media message in the currently open chat to open the viewer."""
    clicked = page.evaluate(
        """
        () => {
            const main = document.querySelector('#main');
            if (!main) return false;
            const imgs = [...main.querySelectorAll('img')].filter(img => {
                const src = img.currentSrc || img.src || '';
                if (!src.startsWith('blob:') && !src.startsWith('https://mmg.whatsapp.net/')) return false;
                const r = img.getBoundingClientRect();
                return r.width >= 120 && r.height >= 120;
            });
            if (!imgs.length) return false;
            const target = imgs[imgs.length - 1];
            target.scrollIntoView({ block: 'center' });
            target.click();
            return true;
        }
        """
    )
    if clicked:
        page.wait_for_timeout(1200)
    return bool(clicked)


def try_click_meta_ai_chat(page: Page) -> bool:
    """Best-effort attempt to open the 'Meta AI' chat if visible in the chat list."""
    try:
        loc = page.locator('span[title="Meta AI"]').first
        if loc.count() > 0 and loc.is_visible():
            loc.click()
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


def is_viewer_open(page: Page) -> bool:
    return bool(
        page.evaluate(
            """
            () => Boolean(
                document.querySelector('[data-icon="download"], [aria-label="Download"], [aria-label="Close"]')
                && document.querySelector('img[src^="blob:"], video[src^="blob:"]')
            )
            """
        )
    )


def run_viewer_mode(
    page: Page,
    store: MediaStore,
    limit: int,
    direction: str,
    delay: float,
    end_patience: int,
) -> tuple[int, int]:
    """Step through the WhatsApp media lightbox viewer and save every unique item."""
    if not is_viewer_open(page):
        print("Media viewer is not open yet — opening the latest image in the active chat...")
        if not open_latest_media_in_chat(page):
            print(
                "Could not auto-open a media item. Please click any photo in the Meta AI chat "
                "so the full-screen viewer opens, then press Enter."
            )
            input()

    key = "ArrowLeft" if direction == "left" else "ArrowRight"
    saved_count = 0
    skipped_count = 0
    seen_urls: set[str] = set()
    stale_steps = 0
    tried_flip = False

    while True:
        if limit > 0 and saved_count >= limit:
            print(f"Reached limit ({limit}). Stopping.")
            break

        media = page.evaluate(FIND_VIEWER_MEDIA_JS)
        if media and media["src"] not in seen_urls:
            seen_urls.add(media["src"])
            stale_steps = 0
            try:
                raw, mime = extract_media(page, media["src"])
                is_new, path = store.save_bytes(raw, mime, media["kind"])
                if is_new:
                    saved_count += 1
                    print(f"[{saved_count}] Saved {path.name} ({len(raw) // 1024} KB)")
                else:
                    skipped_count += 1
                    print(f"[skip dup] {path.name}")
            except Exception as exc:
                print(f"[warn] Failed to extract {media['src'][:40]}...: {exc}")
        else:
            stale_steps += 1
            # If the very first ArrowLeft didn't move (e.g. oldest item already open), try ArrowRight once
            if saved_count + skipped_count <= 1 and not tried_flip and stale_steps >= 2:
                tried_flip = True
                key = "ArrowRight" if key == "ArrowLeft" else "ArrowLeft"
                stale_steps = 0
                print(f"Switching navigation direction to {key}...")
            elif stale_steps >= end_patience:
                print("Reached the end of the media gallery.")
                break

        page.keyboard.press(key)
        time.sleep(delay)

    return saved_count, skipped_count


def run_scroll_mode(
    page: Page,
    store: MediaStore,
    scrolls: int,
    min_size: int,
    delay: float,
) -> tuple[int, int]:
    """Scroll up through the open chat/media pane and save all visible blob media."""
    saved_count = 0
    skipped_count = 0
    seen_urls: set[str] = set()

    for step in range(scrolls + 1):
        items: list[dict[str, Any]] = page.evaluate(FIND_ALL_CHAT_MEDIA_JS, min_size)
        for item in items:
            if item["src"] in seen_urls:
                continue
            seen_urls.add(item["src"])
            try:
                raw, mime = extract_media(page, item["src"])
                is_new, path = store.save_bytes(raw, mime, item["kind"])
                if is_new:
                    saved_count += 1
                    print(f"[{saved_count}] Saved {path.name} ({len(raw) // 1024} KB)")
                else:
                    skipped_count += 1
            except Exception as exc:
                print(f"[warn] Failed blob fetch: {exc}")

        if step == scrolls:
            break

        # Scroll the active chat pane (#main) or media gallery container upward
        page.evaluate(
            """
            () => {
                const scrollers = [
                    document.querySelector('#main [role="application"]')?.parentElement,
                    document.querySelector('#main .copyable-area > div'),
                    document.querySelector('#main'),
                ].filter(Boolean);
                for (const el of scrollers) {
                    if (el.scrollHeight > el.clientHeight) {
                        el.scrollTop = Math.max(0, el.scrollTop - Math.floor(el.clientHeight * 0.85));
                        return;
                    }
                }
                window.scrollBy(0, -800);
            }
            """
        )
        time.sleep(delay)

    return saved_count, skipped_count


def get_whatsapp_page(context: BrowserContext) -> Page:
    for p in context.pages:
        if "web.whatsapp.com" in p.url:
            p.bring_to_front()
            return p
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    return page


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Auto-extract all media from WhatsApp Meta AI chat.")
    ap.add_argument("--mode", choices=["scroll", "viewer"], default="scroll",
                    help="scroll = scroll Meta AI chat & grab all inline media (default); viewer = step through lightbox")
    ap.add_argument("--direction", choices=["left", "right"], default="left",
                    help="Navigation key in viewer mode (default: left = newer -> older)")
    ap.add_argument("--limit", type=int, default=0, help="Max new files to save (0 = unlimited)")
    ap.add_argument("--scrolls", type=int, default=30, help="Scroll steps in scroll mode (default: 30)")
    ap.add_argument("--min-size", type=int, default=200, help="Min width/height px in scroll mode")
    ap.add_argument("--delay", type=float, default=0.7, help="Seconds between steps (default: 0.7)")
    ap.add_argument("--end-patience", type=int, default=4, help="Consecutive empty steps before stopping")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output folder (default: ./downloads)")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="Persistent browser profile folder")
    ap.add_argument("--cdp", type=str, default="auto", help="Chrome CDP URL (default: auto-detect http://127.0.0.1:9222)")
    ap.add_argument("--auto", action="store_true", help="Skip waiting for Enter if Meta AI chat is already open")
    return ap.parse_args()


def detect_cdp(cdp_arg: str) -> str:
    if cdp_arg and cdp_arg != "auto":
        return cdp_arg
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.5) as r:
            if r.status == 200:
                return "http://127.0.0.1:9222"
    except Exception:
        pass
    return ""


def main() -> int:
    args = parse_args()
    store = MediaStore(args.out)
    cdp_url = detect_cdp(args.cdp)

    with sync_playwright() as pw:
        if cdp_url:
            print(f"Connecting to existing Chrome at {cdp_url}...")
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            close_fn = browser.close
        else:
            args.profile.mkdir(parents=True, exist_ok=True)
            print(f"Launching browser with persistent profile: {args.profile}")
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile),
                    channel="chrome",
                    headless=False,
                    viewport={"width": 1400, "height": 900},
                )
            except Exception:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile),
                    headless=False,
                    viewport={"width": 1400, "height": 900},
                )
            close_fn = context.close

        try:
            page = get_whatsapp_page(context)
            print("Waiting for WhatsApp Web...")
            try:
                page.wait_for_selector('#pane-side, #main', timeout=120_000)
            except Exception:
                print("Timed out waiting for WhatsApp Web login. Please scan QR and re-run.")
                return 1

            try_click_meta_ai_chat(page)

            if not args.auto:
                print("\n--- READY ---")
                print("1. Make sure the 'Meta AI' chat is open in WhatsApp Web.")
                print("2. (Optional) Click any image to start from a specific point.")
                input("3. Press ENTER here to start auto-extracting... ")

            if args.mode == "viewer":
                saved, skipped = run_viewer_mode(
                    page, store, args.limit, args.direction, args.delay, args.end_patience
                )
            else:
                saved, skipped = run_scroll_mode(
                    page, store, args.scrolls, args.min_size, args.delay
                )

            print(f"\nDone! Saved {saved} new file(s), skipped {skipped} duplicate(s).")
            print(f"Output folder: {store.out_dir}")
        finally:
            if not cdp_url:
                close_fn()

    return 0


if __name__ == "__main__":
    sys.exit(main())
