"""Live E2E proof that media capture works, with no false positives.

The four checks map to behaviours v0.1.6 got wrong:
  1. a generated image is captured and is a genuine PNG;
  2. reference-image iteration produces a genuinely new asset, not the input
     re-saved;
  3. two consecutive image prompts stay isolated, so a late-delivered image is
     not attributed to the next prompt;
  4. a conversational follow-up captures reply text without inventing media.

Usage:
    .\\.venv\\Scripts\\python.exe tests\\e2e_capture.py
    .\\.venv\\Scripts\\python.exe tests\\e2e_capture.py --keep
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parent.parent / ".playwright-browsers"))

from wa_meta_ai import MetaAIClient  # noqa: E402

PROMPT_A = "Generate an image of a flat crimson hexagon centered on an off-white background, minimal, no text"
PROMPT_B = "Generate an image based on this reference, but change the hexagon to deep navy and add a thin gold outline"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the output directory")
    args = ap.parse_args()

    out = Path(tempfile.mkdtemp(prefix="wa-e2e-"))
    failures: list[str] = []
    seen_hashes: set[str] = set()
    try:
        with MetaAIClient() as client:
            status = client.auth_status()
            print(f"auth: {status}")
            if status != "READY":
                print("SKIP: session is not READY, cannot run live E2E")
                return 2

            client.clear_chat()

            # --- 1. fresh generation -------------------------------------
            res = client.generate(PROMPT_A, output_dir=str(out), name="hex_a", format="png", timeout=150)
            media = res["media"]
            print(f"\n[1] generate -> {len(media)} media, text={res['text'][:60]!r}")
            first_path: Path | None = None
            if not media:
                failures.append("1: no media captured for a valid image prompt")
            else:
                p = Path(media[0]["path"])
                first_path = p
                if not p.exists():
                    failures.append("1: reported path does not exist")
                elif p.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                    failures.append("1: captured file is not a PNG")
                elif media[0]["width"] < 512:
                    failures.append(f"1: unexpected size {media[0]['width']}x{media[0]['height']}")
                else:
                    h = sha(p)
                    seen_hashes.add(h)
                    print(f"    [PASS] {p.name} {media[0]['width']}x{media[0]['height']} sha={h[:12]}")

            # --- 2. reference iteration ----------------------------------
            if first_path and first_path.exists():
                res_b = client.generate(
                    PROMPT_B, output_dir=str(out), name="hex_b", ref_image=str(first_path), format="png", timeout=150
                )
                mb = res_b["media"]
                print(f"\n[2] ref iterate -> {len(mb)} media, text={res_b['text'][:60]!r}")
                if not mb:
                    failures.append("2: no media captured for reference iteration")
                else:
                    pb = Path(mb[0]["path"])
                    hb = sha(pb)
                    if hb == sha(first_path):
                        failures.append("2: captured image is identical to the input (stale re-save)")
                    elif hb in seen_hashes:
                        failures.append("2: captured image duplicates an earlier asset")
                    else:
                        seen_hashes.add(hb)
                        print(f"    [PASS] {pb.name} sha={hb[:12]} differs from input")
            else:
                failures.append("2: cannot test reference iteration without a first image")

            # --- 3. two image prompts stay isolated -----------------------
            # Meta AI sometimes sends a second image several seconds after the
            # first reply. Previously that late image landed after the settle
            # window and was attributed to the *next* prompt.
            res_c = client.generate(
                "Generate an image of a single lime-green circle on black, minimal, no text",
                output_dir=str(out),
                name="circle_c",
                format="png",
                timeout=150,
            )
            print(f"\n[3] second prompt -> {len(res_c['media'])} media, text={res_c['text'][:50]!r}")
            if not res_c["media"]:
                failures.append("3: second prompt captured no media")
            else:
                pc = Path(res_c["media"][0]["path"])
                hc = sha(pc)
                if hc in seen_hashes:
                    failures.append("3: media leaked across prompt boundaries (duplicate asset)")
                else:
                    seen_hashes.add(hc)
                    print(f"    [PASS] {pc.name} sha={hc[:12]} isolated to its own prompt")

            # --- 4. follow-up conversation sends no new image -------------
            res_d = client.generate(
                "thanks, just tell me in one short sentence which shape you drew last",
                output_dir=str(out),
                name="chat_d",
                format="png",
                timeout=90,
            )
            print(f"\n[4] follow-up chat -> {len(res_d['media'])} media, text={res_d['text'][:60]!r}")
            if res_d["media"]:
                failures.append(f"4: chat-only prompt returned media: {[m['filename'] for m in res_d['media']]}")
            elif not res_d["text"]:
                failures.append("4: follow-up captured no reply text at all")
            else:
                print("    [PASS] no media, reply captured")
    finally:
        if args.keep:
            print(f"\noutput kept at: {out}")
        else:
            shutil.rmtree(out, ignore_errors=True)

    print()
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
