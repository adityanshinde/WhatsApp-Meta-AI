"""Full live E2E test covering MetaAIClient SDK, async jobs, batch, and OpenAI endpoint.

Run:
    python tests/e2e_full.py --sdk-only     # tests MetaAIClient directly against WhatsApp Web
    python tests/e2e_full.py --http-only    # tests live wa-meta-ai serve on http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from wa_meta_ai import MetaAIClient

BASE = "http://127.0.0.1:8000"


def _http(method: str, path: str, body: dict | None = None, timeout: int = 180) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def run_sdk_live() -> None:
    print("=== 1. Live MetaAIClient SDK E2E ===")
    with tempfile.TemporaryDirectory() as tmp:
        with MetaAIClient() as client:
            st = client.wait_for_login(timeout=60.0)
            assert st == "READY", st
            assert client.clear_chat() is True
            res = client.generate(
                "Generate an image of a minimalist neon origami crane on dark background",
                output_dir=tmp,
                name="sdk_crane",
                format="png",
                timeout=120,
            )
            assert len(res["media"]) >= 1, res
            p = Path(res["media"][0]["path"])
            assert p.exists() and p.name.startswith("sdk_crane") and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            print(f"  [PASS] MetaAIClient.generate -> {p.name} ({res['media'][0]['width']}x{res['media'][0]['height']}, {res['elapsed_seconds']}s)")


def run_http_live() -> None:
    print("=== 2. Live HTTP API E2E (async job + batch + OpenAI compat) ===")
    with tempfile.TemporaryDirectory() as tmp:
        # A. Async /generate?async=true + /jobs/{id}
        st, raw = _http(
            "POST",
            "/generate?async=true",
            {
                "prompt": "Generate an image of a minimalist geometric fox icon",
                "output_dir": tmp,
                "name": "async_fox",
                "format": "png",
                "timeout": 120,
            },
        )
        assert st == 202, st
        job_id = json.loads(raw)["job_id"]
        job = {}
        for _ in range(90):
            _, jraw = _http("GET", f"/jobs/{job_id}")
            job = json.loads(jraw)
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(2)
        assert job.get("status") == "completed", job
        fox_path = Path(job["result"]["media"][0]["path"])
        assert fox_path.exists() and fox_path.name.startswith("async_fox")
        print(f"  [PASS] Async /generate + /jobs/{job_id} -> {fox_path.name}")

        # B. /generate/batch with custom names + clear_after=True
        st, raw = _http(
            "POST",
            "/generate/batch",
            {
                "prompts": [
                    "Generate an image of a minimalist blue whale illustration",
                    "Generate an image of a minimalist red panda illustration",
                ],
                "output_dir": tmp,
                "names": ["batch_whale", "batch_panda"],
                "format": "png",
                "clear_after": True,
                "async_mode": False,
                "timeout": 120,
            },
            timeout=300,
        )
        assert st == 200, st
        batch = json.loads(raw)
        assert batch["cleared_after"] is True and batch["total_media"] >= 2, batch
        print(f"  [PASS] /generate/batch -> {[m['filename'] for m in batch['media']]}, cleared_after=True")

        # C. OpenAI-compatible /v1/images/generations
        st, raw = _http(
            "POST",
            "/v1/images/generations",
            {"prompt": "Generate an image of a minimalist golden compass icon", "n": 1, "response_format": "url"},
            timeout=150,
        )
        assert st == 200, st
        oai = json.loads(raw)
        img_url = oai["data"][0]["url"]
        with urllib.request.urlopen(img_url, timeout=30) as r:
            png_bytes = r.read()
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        print(f"  [PASS] /v1/images/generations -> {img_url} ({len(png_bytes)} bytes)")

        # Cleanup chat
        _http("POST", "/chat/clear", {"chat": "Meta AI"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk-only", action="store_true")
    ap.add_argument("--http-only", action="store_true")
    args = ap.parse_args()
    if args.sdk_only:
        run_sdk_live()
    elif args.http_only:
        run_http_live()
    else:
        run_sdk_live()
        run_http_live()
    return 0


if __name__ == "__main__":
    sys.exit(main())
