"""Media extraction, magic-byte sniffing, format conversion, and manifest store."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

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
    return { mime: blob.type || '', size: bytes.length, b64: btoa(binary) };
}
"""


def sniff_ext(raw: bytes, mime: str = "", kind: str = "image") -> str:
    """Detect real file extension from magic bytes, ignoring application/octet-stream."""
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


def convert_image(raw: bytes, target_format: str) -> tuple[bytes, str, int, int]:
    """Convert image bytes to target_format (png, jpg, webp) and return (bytes, ext, w, h)."""
    fmt = target_format.lower().strip().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"

    with Image.open(io.BytesIO(raw)) as img:
        w, h = img.size
        orig_ext = sniff_ext(raw)
        if not fmt or fmt == "original" or f".{fmt}" == orig_ext:
            return raw, orig_ext, w, h

        pil_fmt = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}.get(fmt)
        if not pil_fmt:
            return raw, orig_ext, w, h

        out_img = img.convert("RGB") if pil_fmt == "JPEG" and img.mode in ("RGBA", "P") else img
        buf = io.BytesIO()
        out_img.save(buf, format=pil_fmt)
        return buf.getvalue(), f".{fmt}", w, h


def download_url_bytes(url: str, timeout: int = 30) -> tuple[bytes, str]:
    """Download media bytes from a direct mmg.whatsapp.net URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


async def extract_blob_bytes(page: Any, blob_url: str) -> tuple[bytes, str]:
    """Fetch a browser blob: URL from inside the Playwright page."""
    data = await page.evaluate(FETCH_BLOB_JS, blob_url)
    return base64.b64decode(data["b64"]), data.get("mime", "")


class MediaStore:
    """Deduplicated media saver with optional series subfolders and zero-clutter default."""

    def __init__(
        self, base_dir: Path, series: str = "", save_manifest: bool = False, create: bool = True
    ) -> None:
        safe_series = "".join(c for c in series if c.isalnum() or c in ("-", "_", " ")).strip()
        self.base_dir = Path(base_dir)
        self.series = safe_series
        self.save_manifest = save_manifest
        self.out_dir = self.base_dir / safe_series if safe_series else self.base_dir
        if create:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.out_dir / "manifest.json"
        self.hashes: dict[str, str] = {}
        self.short_hashes: dict[str, str] = {}
        self.items: list[dict[str, Any]] = []
        self.counter = 0
        self._load()

    def _load(self) -> None:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                self.hashes = data.get("hashes", {})
                self.items = data.get("items", [])
                self.counter = int(data.get("counter", len(self.hashes)))
            except Exception:
                pass
        if not self.out_dir.exists():
            return
        # Index existing media files in out_dir so dedup/numbering works even without manifest.json
        for p in sorted(self.out_dir.iterdir()):
            if not p.is_file() or p.name == "manifest.json":
                continue
            stem_parts = p.stem.split("_")
            if len(stem_parts) >= 4 and stem_parts[0] == "meta" and stem_parts[1] == "ai":
                if stem_parts[2].isdigit():
                    self.counter = max(self.counter, int(stem_parts[2]))
                self.short_hashes.setdefault(stem_parts[3], p.name)

    def _save_manifest(self) -> None:
        if not self.save_manifest:
            return
        payload = {"counter": self.counter, "hashes": self.hashes, "items": self.items}
        self.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def save_media(
        self,
        raw: bytes,
        mime: str = "",
        kind: str = "image",
        target_format: str = "png",
        prompt: str = "",
        chat: str = "Meta AI",
        source_url: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """Save media bytes with deduplication and optional image format conversion."""
        digest = hashlib.sha256(raw).hexdigest()
        existing_name = self.hashes.get(digest) or self.short_hashes.get(digest[:8])
        if existing_name:
            existing_path = (self.out_dir / existing_name).resolve()
            if existing_path.exists():
                for item in self.items:
                    if item.get("filename") == existing_name:
                        return False, item
                rel_url = f"/media/{self.series}/{existing_name}" if self.series else f"/media/{existing_name}"
                w, h = 0, 0
                if kind == "image":
                    try:
                        with Image.open(existing_path) as im:
                            w, h = im.size
                    except Exception:
                        pass
                return False, {
                    "filename": existing_name,
                    "url": rel_url,
                    "path": str(existing_path),
                    "series": self.series,
                    "chat": chat,
                    "prompt": prompt,
                    "kind": kind,
                    "width": w,
                    "height": h,
                    "bytes": existing_path.stat().st_size,
                    "sha256": digest,
                    "source_url": source_url,
                }

        width = 0
        height = 0
        if kind == "image":
            try:
                raw_out, ext, width, height = convert_image(raw, target_format)
            except Exception:
                raw_out = raw
                ext = sniff_ext(raw, mime, kind)
        else:
            raw_out = raw
            ext = sniff_ext(raw, mime, kind)

        self.counter += 1
        filename = f"meta_ai_{self.counter:04d}_{digest[:8]}{ext}"
        dest = (self.out_dir / filename).resolve()
        dest.write_bytes(raw_out)

        rel_url = f"/media/{self.series}/{filename}" if self.series else f"/media/{filename}"
        record = {
            "filename": filename,
            "url": rel_url,
            "path": str(dest),
            "series": self.series,
            "chat": chat,
            "prompt": prompt,
            "kind": kind,
            "width": width,
            "height": height,
            "bytes": len(raw_out),
            "sha256": digest,
            "source_url": source_url,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.hashes[digest] = filename
        self.short_hashes[digest[:8]] = filename
        self.items.append(record)
        self._save_manifest()
        return True, record

    def list_items(self, query: str = "", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        items = self.items
        if not items and self.out_dir.exists():
            items = [
                {
                    "filename": p.name,
                    "url": f"/media/{self.series}/{p.name}" if self.series else f"/media/{p.name}",
                    "path": str(p.resolve()),
                    "bytes": p.stat().st_size,
                }
                for p in sorted(self.out_dir.iterdir())
                if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4")
            ]
        if query:
            q = query.lower()
            items = [i for i in items if q in i.get("prompt", "").lower() or q in i.get("filename", "").lower()]
        return items[offset : offset + limit]
