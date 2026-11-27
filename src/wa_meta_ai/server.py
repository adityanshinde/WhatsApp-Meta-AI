"""FastAPI REST API for WhatsApp Meta AI image generation and extraction."""

from __future__ import annotations

import base64
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .browser import WhatsAppBrowser
from .config import Settings
from .extractor import MediaStore
from .jobs import JobManager


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    chat: str = "Meta AI"
    series: str = ""
    output_dir: str = ""
    ref_image: str = ""
    name: str = ""
    format: str = "png"
    target_count: int = Field(default=0, ge=0, le=200)
    auto_continue: bool = True
    continue_text: str = "continue"
    max_rounds: int = Field(default=20, ge=1, le=50)
    timeout: int = Field(default=90, ge=10, le=300)
    settle_sec: float = Field(default=3.0, ge=0.5, le=15.0)
    async_mode: bool = False
    webhook_url: str | None = None


class BatchGenerateRequest(BaseModel):
    prompts: list[str] = Field(..., min_length=1)
    chat: str = "Meta AI"
    series: str = ""
    output_dir: str = ""
    ref_image: str = ""
    names: list[str] = Field(default_factory=list)
    format: str = "png"
    clear_after: bool = False
    timeout: int = Field(default=90, ge=10, le=300)
    settle_sec: float = Field(default=3.0, ge=0.5, le=15.0)
    async_mode: bool = True
    webhook_url: str | None = None


class ClearChatRequest(BaseModel):
    chat: str = "Meta AI"


class ExtractRequest(BaseModel):
    chat: str = "Meta AI"
    series: str = ""
    output_dir: str = ""
    scrolls: int = Field(default=30, ge=0, le=500)
    limit: int = Field(default=0, ge=0)
    format: str = "png"


class OpenAIImageRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    n: int = 1
    size: str = "1600x1600"
    response_format: str = "url"


def create_app(settings: Settings | None = None, start_browser: bool = True) -> FastAPI:
    """Build the FastAPI application with optional browser lifecycle."""
    cfg = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        browser = WhatsAppBrowser(cfg)
        app.state.browser = browser
        app.state.settings = cfg
        app.state.jobs = JobManager()
        if start_browser:
            await browser.start()
        try:
            yield
        finally:
            if start_browser:
                await browser.stop()

    app = FastAPI(title="WhatsApp Meta AI API", version="0.1.3", lifespan=lifespan)

    def verify_key(
        x_api_key: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> None:
        if not cfg.api_key:
            return
        token = x_api_key or (authorization[7:].strip() if authorization.startswith("Bearer ") else "")
        if token != cfg.api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    def get_browser(request: Request) -> WhatsAppBrowser:
        return request.app.state.browser

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>WhatsApp Meta AI API — Setup</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 640px; margin: 48px auto; padding: 0 16px; color: #111; }
    .card { border: 1px solid #ddd; border-radius: 12px; padding: 24px; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 999px; font-weight: 600; font-size: 13px; }
    .ready { background: #dcfce7; color: #166534; }
    .qr { background: #fef9c3; color: #854d0e; }
    img { display: block; margin: 16px 0; border: 1px solid #eee; border-radius: 8px; max-width: 280px; }
    a { color: #0969da; }
  </style>
</head>
<body>
  <div class="card">
    <h2>WhatsApp Meta AI API</h2>
    <p>Status: <span id="st" class="badge qr">Checking...</span></p>
    <div id="qr-box" style="display:none">
      <p><b>Link WhatsApp:</b> Open WhatsApp on your phone &rarr; <b>Linked Devices</b> &rarr; <b>Link a Device</b> &rarr; scan this QR:</p>
      <img id="qr-img" src="/auth/qr" alt="WhatsApp QR" />
    </div>
    <div id="ready-box" style="display:none">
      <p>WhatsApp is connected and ready.</p>
      <ul>
        <li><a href="/docs">Interactive API Docs (/docs)</a></li>
        <li><a href="/media">Extracted Media JSON (/media)</a></li>
      </ul>
    </div>
  </div>
  <script>
    async function refresh() {
      try {
        const r = await fetch('/health');
        const d = await r.json();
        const st = document.getElementById('st');
        st.textContent = d.auth_status;
        if (d.auth_status === 'READY') {
          st.className = 'badge ready';
          document.getElementById('qr-box').style.display = 'none';
          document.getElementById('ready-box').style.display = 'block';
        } else {
          st.className = 'badge qr';
          document.getElementById('ready-box').style.display = 'none';
          document.getElementById('qr-box').style.display = 'block';
          document.getElementById('qr-img').src = '/auth/qr?t=' + Date.now();
        }
      } catch {}
    }
    refresh();
    setInterval(refresh, 4000);
  </script>
</body>
</html>"""

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        browser: WhatsAppBrowser = get_browser(request)
        status = await browser.auth_status() if browser._page else "NOT_STARTED"
        return {"ok": True, "auth_status": status}

    @app.get("/debug/ui")
    async def debug_ui(request: Request) -> dict[str, Any]:
        return await get_browser(request).debug_ui()

    @app.get("/auth/status")
    async def auth_status(request: Request, _: None = Depends(verify_key)) -> dict[str, str]:
        return {"status": await get_browser(request).auth_status()}

    @app.get("/auth/qr")
    async def auth_qr(request: Request, _: None = Depends(verify_key)) -> Response:
        png = await get_browser(request).qr_screenshot()
        if not png:
            raise HTTPException(status_code=404, detail="QR code not present (already logged in or loading)")
        return Response(content=png, media_type="image/png")

    @app.post("/generate")
    async def generate(
        body: GenerateRequest,
        request: Request,
        async_q: bool = Query(default=False, alias="async"),
        _: None = Depends(verify_key),
    ) -> Any:
        browser = get_browser(request)
        if body.async_mode or async_q:
            jobs: JobManager = request.app.state.jobs
            job = jobs.submit(
                "generate",
                lambda: browser.generate(
                    prompt=body.prompt,
                    chat=body.chat,
                    timeout=body.timeout,
                    settle_sec=body.settle_sec,
                    fmt=body.format,
                    series=body.series,
                    output_dir=body.output_dir,
                    ref_image=body.ref_image,
                    name=body.name,
                    target_count=body.target_count,
                    auto_continue=body.auto_continue,
                    continue_text=body.continue_text,
                    max_rounds=body.max_rounds,
                ),
                webhook_url=body.webhook_url,
            )
            return JSONResponse(status_code=202, content=job.to_dict())

        try:
            return await browser.generate(
                prompt=body.prompt,
                chat=body.chat,
                timeout=body.timeout,
                settle_sec=body.settle_sec,
                fmt=body.format,
                series=body.series,
                output_dir=body.output_dir,
                ref_image=body.ref_image,
                name=body.name,
                target_count=body.target_count,
                auto_continue=body.auto_continue,
                continue_text=body.continue_text,
                max_rounds=body.max_rounds,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/generate/batch")
    async def generate_batch(
        body: BatchGenerateRequest,
        request: Request,
        _: None = Depends(verify_key),
    ) -> Any:
        browser = get_browser(request)
        if body.async_mode:
            jobs: JobManager = request.app.state.jobs
            job = jobs.submit(
                "generate_batch",
                lambda: browser.generate_batch(
                    prompts=body.prompts,
                    chat=body.chat,
                    series=body.series,
                    output_dir=body.output_dir,
                    ref_image=body.ref_image,
                    names=body.names,
                    fmt=body.format,
                    clear_after=body.clear_after,
                    timeout=body.timeout,
                    settle_sec=body.settle_sec,
                ),
                webhook_url=body.webhook_url,
            )
            return JSONResponse(status_code=202, content=job.to_dict())

        try:
            return await browser.generate_batch(
                prompts=body.prompts,
                chat=body.chat,
                series=body.series,
                output_dir=body.output_dir,
                ref_image=body.ref_image,
                names=body.names,
                fmt=body.format,
                clear_after=body.clear_after,
                timeout=body.timeout,
                settle_sec=body.settle_sec,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/jobs")
    async def list_jobs(request: Request, limit: int = 50, _: None = Depends(verify_key)) -> dict[str, Any]:
        jobs: JobManager = request.app.state.jobs
        return {"jobs": jobs.list_jobs(limit=limit)}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request, _: None = Depends(verify_key)) -> dict[str, Any]:
        jobs: JobManager = request.app.state.jobs
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()

    @app.post("/chat/clear")
    async def clear_chat(body: ClearChatRequest, request: Request, _: None = Depends(verify_key)) -> dict[str, Any]:
        try:
            await get_browser(request).clear_chat(chat=body.chat)
            return {"cleared": True, "chat": body.chat}
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/extract")
    async def extract(body: ExtractRequest, request: Request, _: None = Depends(verify_key)) -> dict[str, Any]:
        try:
            return await get_browser(request).extract_history(
                chat=body.chat,
                scrolls=body.scrolls,
                limit=body.limit,
                fmt=body.format,
                series=body.series,
                output_dir=body.output_dir,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/media")
    async def list_media(
        series: str = "",
        q: str = "",
        limit: int = 100,
        offset: int = 0,
        _: None = Depends(verify_key),
    ) -> dict[str, Any]:
        store = MediaStore(cfg.downloads_dir, series=series, create=False)
        return {"series": store.series, "items": store.list_items(query=q, limit=limit, offset=offset)}

    def _resolve_media_file(subpath: Path) -> FileResponse:
        base = cfg.downloads_dir.resolve()
        target = (base / subpath).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(status_code=404, detail="Media file not found")
        return FileResponse(target)

    @app.get("/media/{filename}")
    async def get_media(filename: str, _: None = Depends(verify_key)) -> FileResponse:
        return _resolve_media_file(Path(filename))

    @app.get("/media/{series}/{filename}")
    async def get_series_media(series: str, filename: str, _: None = Depends(verify_key)) -> FileResponse:
        return _resolve_media_file(Path(series) / filename)

    @app.post("/v1/images/generations")
    async def openai_images(
        body: OpenAIImageRequest,
        request: Request,
        _: None = Depends(verify_key),
    ) -> dict[str, Any]:
        prompt = body.prompt
        if body.n > 1 and str(body.n) not in prompt:
            prompt = f"Generate {body.n} different images: {prompt}"
        result = await get_browser(request).generate(
            prompt=prompt, fmt="png", target_count=body.n, auto_continue=True
        )
        data: list[dict[str, Any]] = []
        base_url = str(request.base_url).rstrip("/")
        for item in result["media"]:
            if body.response_format == "b64_json":
                raw = Path(item["path"]).read_bytes()
                data.append({"b64_json": base64.b64encode(raw).decode("ascii")})
            else:
                data.append({"url": f"{base_url}{item['url']}"})
        return {"created": int(time.time()), "data": data}

    return app
