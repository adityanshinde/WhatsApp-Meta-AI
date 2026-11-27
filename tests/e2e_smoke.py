"""Live E2E smoke test against a running wa-meta-ai server (http://127.0.0.1:8000)."""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(path: str, body: dict | None = None, timeout: int = 120) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    r = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read()


def main() -> int:
    st, raw = req("/auth/status")
    print("1. /auth/status:", st, raw.decode())

    st, raw = req(
        "/generate",
        {
            "prompt": "Generate 2 cute minimalist claymation space cats",
            "series": "e2e_test",
            "format": "png",
            "target_count": 2,
        },
        timeout=120,
    )
    gen = json.loads(raw)
    print(
        "2. /generate:",
        st,
        "rounds:",
        gen.get("rounds"),
        "media_count:",
        len(gen["media"]),
        "elapsed:",
        gen["elapsed_seconds"],
    )
    for m in gen["media"]:
        print("   -", m["filename"], f"{m['width']}x{m['height']}", m["bytes"], "bytes")

    st, raw = req("/media?series=e2e_test")
    print("3. /media:", st, "items:", len(json.loads(raw)["items"]))

    if gen["media"]:
        st, img_raw = req(gen["media"][0]["url"])
        print("4. GET media file:", st, "bytes:", len(img_raw), "png_magic:", img_raw[:8] == b"\x89PNG\r\n\x1a\n")

    st, raw = req("/chat/clear", {"chat": "Meta AI"})
    print("5. /chat/clear:", st, raw.decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
