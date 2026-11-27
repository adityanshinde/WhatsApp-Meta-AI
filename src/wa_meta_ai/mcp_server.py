"""MCP (Model Context Protocol) server for wa-meta-ai."""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .browser import WhatsAppBrowser
from .config import Settings
from .extractor import MediaStore


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Create a FastMCP server exposing WhatsApp Meta AI tools.

    If `wa-meta-ai serve` is already running on `http://<host>:<port>`, tool calls
    automatically proxy to it so Chromium's `wa_profile/` is never locked by two processes.
    """
    cfg = settings or Settings()
    mcp = FastMCP("wa-meta-ai")
    state: dict[str, WhatsAppBrowser | None] = {"browser": None}
    base_url = f"http://{cfg.host}:{cfg.port}"

    async def _try_http(method: str, path: str, json_body: dict[str, Any] | None = None) -> dict[str, Any] | None:
        headers = {"X-API-Key": cfg.api_key} if cfg.api_key else {}
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=600.0, headers=headers) as client:
                r = await client.request(method, path, json=json_body)
                if r.status_code < 400:
                    return r.json()
        except httpx.HTTPError:
            return None
        return None

    async def get_browser() -> WhatsAppBrowser:
        if state["browser"] is None:
            b = WhatsAppBrowser(cfg)
            await b.start()
            state["browser"] = b
        return state["browser"]

    @mcp.tool()
    async def auth_status() -> dict[str, str]:
        """Check WhatsApp Web login status (READY, NEEDS_QR, or DISCONNECTED)."""
        if res := await _try_http("GET", "/auth/status"):
            return res
        b = await get_browser()
        return {"status": await b.auth_status()}

    @mcp.tool()
    async def generate_images(
        prompt: str,
        output_dir: str = "",
        series: str = "",
        ref_image: str = "",
        name: str = "",
        format: str = "png",
        target_count: int = 0,
        auto_continue: bool = True,
        timeout: int = 120,
        chat: str = "Meta AI",
    ) -> dict[str, Any]:
        """Send a prompt (start with 'Generate an image of ...' for reliable image output) to Meta AI."""
        payload = {
            "prompt": prompt,
            "chat": chat,
            "series": series,
            "output_dir": output_dir,
            "ref_image": ref_image,
            "name": name,
            "format": format,
            "target_count": target_count,
            "auto_continue": auto_continue,
            "timeout": timeout,
        }
        if res := await _try_http("POST", "/generate", payload):
            return res
        b = await get_browser()
        return await b.generate(
            prompt=prompt,
            chat=chat,
            series=series,
            output_dir=output_dir,
            ref_image=ref_image,
            name=name,
            fmt=format,
            target_count=target_count,
            auto_continue=auto_continue,
            timeout=timeout,
        )

    @mcp.tool()
    async def generate_batch(
        prompts: list[str],
        output_dir: str = "",
        series: str = "",
        ref_image: str = "",
        names: list[str] | None = None,
        format: str = "png",
        clear_after: bool = False,
        timeout: int = 120,
        chat: str = "Meta AI",
    ) -> dict[str, Any]:
        """Run prompts sequentially into output_dir (with optional per-prompt names). For 3+ prompts, prefer calling generate_images one by one to avoid MCP client timeouts."""
        payload = {
            "prompts": prompts,
            "chat": chat,
            "series": series,
            "output_dir": output_dir,
            "ref_image": ref_image,
            "names": names or [],
            "format": format,
            "clear_after": clear_after,
            "timeout": timeout,
            "async_mode": False,
        }
        if res := await _try_http("POST", "/generate/batch", payload):
            return res
        b = await get_browser()
        return await b.generate_batch(
            prompts=prompts,
            chat=chat,
            series=series,
            output_dir=output_dir,
            ref_image=ref_image,
            names=names,
            fmt=format,
            clear_after=clear_after,
            timeout=timeout,
        )

    @mcp.tool()
    async def extract_history(
        chat: str = "Meta AI",
        output_dir: str = "",
        scrolls: int = 30,
        limit: int = 0,
        series: str = "",
        format: str = "png",
    ) -> dict[str, Any]:
        """Scroll a WhatsApp Web chat upward and download all inline media into output_dir."""
        payload = {
            "chat": chat,
            "output_dir": output_dir,
            "scrolls": scrolls,
            "limit": limit,
            "series": series,
            "format": format,
        }
        if res := await _try_http("POST", "/extract", payload):
            return res
        b = await get_browser()
        return await b.extract_history(
            chat=chat,
            scrolls=scrolls,
            limit=limit,
            series=series,
            output_dir=output_dir,
            fmt=format,
        )

    @mcp.tool()
    async def clear_chat(chat: str = "Meta AI") -> dict[str, Any]:
        """Clear all messages in the Meta AI chat to start a fresh series."""
        if res := await _try_http("POST", "/chat/clear", {"chat": chat}):
            return res
        b = await get_browser()
        await b.clear_chat(chat=chat)
        return {"cleared": True, "chat": chat}

    @mcp.tool()
    async def list_media(series: str = "", query: str = "", limit: int = 50) -> dict[str, Any]:
        """List extracted media files from downloads/ (optionally filtered by series or prompt)."""
        store = MediaStore(cfg.downloads_dir, series=series, create=False)
        return {"series": store.series, "items": store.list_items(query=query, limit=limit)}

    return mcp


def run_mcp(settings: Settings | None = None, transport: str = "stdio") -> None:
    """Run the MCP server over stdio or streamable-http."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    server = create_mcp_server(settings)
    server.run(transport=transport)
