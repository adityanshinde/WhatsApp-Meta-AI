"""Self-contained unit and API tests for wa_meta_ai (CI-safe, no downloads/ or browser needed)."""

import io
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from PIL import Image

from wa_meta_ai import MetaAIClient
from wa_meta_ai.config import Settings
from wa_meta_ai.extractor import MediaStore, convert_image, sniff_ext
from wa_meta_ai.server import create_app


def _make_webp(width: int = 64, height: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 140, 220)).save(buf, format="WEBP")
    return buf.getvalue()


def test_sniff_ext_and_convert() -> None:
    webp = _make_webp(64, 48)
    assert sniff_ext(webp, "application/octet-stream") == ".webp"
    png, ext, w, h = convert_image(webp, "png")
    assert ext == ".png"
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert (w, h) == (64, 48)

    with tempfile.TemporaryDirectory() as tmp:
        s1 = MediaStore(Path(tmp), save_manifest=False)
        is_new, rec = s1.save_media(webp, target_format="png")
        assert is_new and rec["filename"].endswith(".png")
        assert not (Path(tmp) / "manifest.json").exists()

        # Re-open store without manifest and verify duplicate returns full record shape
        s2 = MediaStore(Path(tmp), save_manifest=False)
        is_new2, dup = s2.save_media(webp, target_format="png")
        assert not is_new2
        assert dup["url"] == rec["url"]
        assert dup["width"] == 64 and dup["height"] == 48

        # Custom filename support
        is_n1, r1 = s2.save_media(_make_webp(32, 32), target_format="png", name="hero_scene")
        assert is_n1 and r1["filename"] == "hero_scene.png"
        is_n2, r2 = s2.save_media(_make_webp(40, 40), target_format="png", name="hero_scene")
        assert is_n2 and r2["filename"] == "hero_scene_01.png"


def test_async_jobs_batch_and_auth() -> None:
    webp = _make_webp(32, 32)
    with tempfile.TemporaryDirectory() as tmp:
        dl_dir = Path(tmp)
        store = MediaStore(dl_dir, series="e2e_test", save_manifest=True)
        _, saved = store.save_media(webp, target_format="png", prompt="space cats")

        app = create_app(
            Settings(downloads_dir=dl_dir, api_key="secret123"),
            start_browser=False,
        )
        with TestClient(app) as client:
            home = client.get("/")
            assert home.status_code == 200 and "WhatsApp Meta AI API" in home.text
            assert client.get("/health").status_code == 200

            # Protected endpoints enforce API key (including /media/{series}/{filename})
            assert client.get("/jobs").status_code == 401
            assert client.get(saved["url"]).status_code == 401
            headers = {"X-API-Key": "secret123"}
            assert client.get("/jobs", headers=headers).status_code == 200
            assert client.get(saved["url"], headers=headers).status_code == 200

            # Read-only /media listing must not create nonexistent series directory
            assert client.get("/media?series=nonexistent", headers=headers).status_code == 200
            assert not (dl_dir / "nonexistent").exists()

            # Async /generate and /generate/batch
            app.state.browser.generate = AsyncMock(
                return_value={"prompt": "test", "text": "ok", "media": [], "rounds": 1, "elapsed_seconds": 0.1}
            )
            app.state.browser.generate_batch = AsyncMock(
                return_value={"series": "s1", "total_media": 0, "media": [], "cleared_after": True}
            )

            r = client.post("/generate?async=true", json={"prompt": "test"}, headers=headers)
            assert r.status_code == 202
            job_id = r.json()["job_id"]

            j = client.get(f"/jobs/{job_id}", headers=headers)
            assert j.status_code == 200
            assert j.json()["status"] in ("queued", "running", "completed")

            rb = client.post(
                "/generate/batch",
                json={"prompts": ["p1", "p2"], "series": "s1", "clear_after": True},
                headers=headers,
            )
            assert rb.status_code == 202
            assert rb.json()["kind"] == "generate_batch"

            rm = client.get("/media?series=e2e_test&q=cats", headers=headers)
            assert rm.status_code == 200
            assert len(rm.json()["items"]) == 1


if __name__ == "__main__":
    test_sniff_ext_and_convert()
    test_async_jobs_batch_and_auth()
    print("All API & extractor tests passed!")
