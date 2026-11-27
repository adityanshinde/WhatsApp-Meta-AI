"""Command-line interface for wa-meta-ai."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import uvicorn

from .browser import WhatsAppBrowser
from .config import Settings
from .server import create_app


def _build_settings(args: argparse.Namespace) -> Settings:
    s = Settings()
    if args.profile:
        s.profile_dir = Path(args.profile)
    if args.downloads:
        s.downloads_dir = Path(args.downloads)
    if args.cdp:
        s.cdp_url = args.cdp
    if args.format:
        s.default_format = args.format
    if getattr(args, "headless", False):
        s.headless = True
    return s


async def _run_once(args: argparse.Namespace) -> int:
    settings = _build_settings(args)
    browser = WhatsAppBrowser(settings)
    await browser.start()
    try:
        if await browser.auth_status() != "READY":
            print("WhatsApp Web is not logged in. Scan QR in the browser window, then press ENTER...")
            input()

        if args.cmd == "generate":
            res = await browser.generate(
                prompt=args.prompt,
                chat=args.chat,
                series=args.series,
                ref_image=args.ref,
                fmt=args.format,
                target_count=args.target_count,
                auto_continue=not args.no_auto_continue,
                timeout=args.timeout,
            )
        elif args.cmd == "extract":
            res = await browser.extract_history(
                chat=args.chat,
                scrolls=args.scrolls,
                limit=args.limit,
                series=args.series,
                fmt=args.format,
            )
        elif args.cmd == "clear":
            await browser.clear_chat(chat=args.chat)
            res = {"cleared": True, "chat": args.chat}
        else:
            return 1

        print(json.dumps(res, indent=2))
        return 0
    finally:
        await browser.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wa-meta-ai", description="WhatsApp Meta AI API & CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--profile", default=None, help="Persistent browser profile folder")
        p.add_argument("--downloads", default=None, help="Output downloads folder")
        p.add_argument("--cdp", default=None, help="Optional Chrome CDP URL")
        p.add_argument("--format", default=None, choices=["png", "jpg", "webp", "original"], help="Output format")

    p_serve = sub.add_parser("serve", help="Start the FastAPI server")
    add_common(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--headless", action="store_true")

    p_gen = sub.add_parser("generate", help="Send a prompt to Meta AI and download generated images")
    add_common(p_gen)
    p_gen.add_argument("prompt", help="Prompt text")
    p_gen.add_argument("--chat", default="Meta AI")
    p_gen.add_argument("--series", default="", help="Optional subfolder under downloads/")
    p_gen.add_argument("--ref", default="", help="Optional reference image path to attach")
    p_gen.add_argument("--target-count", type=int, default=0, help="Auto-continue until N images are saved")
    p_gen.add_argument("--no-auto-continue", action="store_true", help="Disable auto-continue on batched replies")
    p_gen.add_argument("--timeout", type=int, default=90)

    p_ext = sub.add_parser("extract", help="Scroll chat history and download all media")
    add_common(p_ext)
    p_ext.add_argument("--chat", default="Meta AI")
    p_ext.add_argument("--series", default="")
    p_ext.add_argument("--scrolls", type=int, default=30)
    p_ext.add_argument("--limit", type=int, default=0)

    p_clr = sub.add_parser("clear", help="Clear messages in the Meta AI chat")
    add_common(p_clr)
    p_clr.add_argument("--chat", default="Meta AI")

    p_mcp = sub.add_parser("mcp", help="Start the MCP server for AI agents (stdio or streamable-http)")
    add_common(p_mcp)
    p_mcp.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    p_mcp.add_argument("--headless", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import run_mcp

        run_mcp(settings=_build_settings(args), transport=args.transport)
        return 0

    if args.cmd == "serve":
        settings = _build_settings(args)
        settings.host = args.host
        settings.port = args.port
        print(f"WhatsApp Meta AI API -> http://{args.host}:{args.port}")
        print(f"QR / Setup UI        -> http://{args.host}:{args.port}/")
        print(f"API Docs (Swagger)   -> http://{args.host}:{args.port}/docs")
        print(f"Profile dir          -> {settings.profile_dir}")
        app = create_app(settings=settings, start_browser=True)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    return asyncio.run(_run_once(args))


if __name__ == "__main__":
    sys.exit(main())
