"""
Pure-Python Media Downloader for WhatsApp Meta AI URLs (no Playwright needed).

Usage:
    python download_urls.py <url1> <url2> ...
    python download_urls.py --file urls.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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


def sniff_ext(raw: bytes, mime: str, kind: str = "image") -> str:
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
        self.manifest_path.write_text(
            json.dumps({"counter": self.counter, "hashes": self.hashes}, indent=2),
            encoding="utf-8",
        )
        return True, dest


def download_url(url: str, store: MediaStore) -> tuple[bool, Path]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        ct = resp.headers.get("Content-Type", "")
    return store.save_bytes(raw, ct, "video" if ct.startswith("video/") else "image")


def main() -> int:
    ap = argparse.ArgumentParser(description="Download WhatsApp Meta AI media URLs to downloads/.")
    ap.add_argument("urls", nargs="*", help="Media URLs to download")
    ap.add_argument("--file", type=Path, help="Text file containing one URL per line")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output folder")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file and args.file.exists():
        urls.extend(line.strip() for line in args.file.read_text(encoding="utf-8").splitlines() if line.strip())

    if not urls:
        print("No URLs provided.", file=sys.stderr)
        return 1

    store = MediaStore(args.out)
    saved = 0
    skipped = 0
    for url in urls:
        try:
            is_new, path = download_url(url, store)
            if is_new:
                saved += 1
                print(f"[saved] {path.name}")
            else:
                skipped += 1
                print(f"[dup]   {path.name}")
        except Exception as exc:
            print(f"[error] {url[:60]}... -> {exc}", file=sys.stderr)

    print(f"Done: {saved} saved, {skipped} duplicate(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
