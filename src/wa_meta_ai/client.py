"""Synchronous Python SDK wrapper around WhatsAppBrowser."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from .browser import WhatsAppBrowser
from .config import Settings


class MetaAIClient:
    """High-level synchronous Python SDK for WhatsApp Meta AI.

    Example:
        with MetaAIClient() as client:
            client.wait_for_login()
            res = client.generate("Cute futuristic robot", format="png")
    """

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        downloads_dir: str | Path | None = None,
        cdp_url: str = "",
        default_format: str = "png",
        headless: bool = False,
    ) -> None:
        self.settings = Settings()
        if profile_dir is not None:
            self.settings.profile_dir = Path(profile_dir)
        if downloads_dir is not None:
            self.settings.downloads_dir = Path(downloads_dir)
        if cdp_url:
            self.settings.cdp_url = cdp_url
        if default_format:
            self.settings.default_format = default_format
        self.settings.headless = headless

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._browser = WhatsAppBrowser(self.settings)
        self._started = False

    def _run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def start(self) -> MetaAIClient:
        if not self._started:
            self._run(self._browser.start())
            self._started = True
        return self

    def close(self) -> None:
        if self._started:
            self._run(self._browser.stop())
            self._started = False
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    def __enter__(self) -> MetaAIClient:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.close()

    def auth_status(self) -> str:
        return self._run(self._browser.auth_status())

    def wait_for_login(self, timeout: float = 120.0) -> str:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.auth_status()
            if st == "READY":
                return st
            time.sleep(1.5)
        raise TimeoutError("Timed out waiting for WhatsApp Web QR login")

    def generate(
        self,
        prompt: str,
        *,
        chat: str = "Meta AI",
        series: str = "",
        output_dir: str = "",
        ref_image: str = "",
        name: str = "",
        format: str = "",
        target_count: int = 0,
        auto_continue: bool = True,
        timeout: int = 120,
    ) -> dict[str, Any]:
        return self._run(
            self._browser.generate(
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
        )

    def generate_batch(
        self,
        prompts: list[str],
        *,
        chat: str = "Meta AI",
        series: str = "",
        output_dir: str = "",
        ref_image: str = "",
        names: list[str] | None = None,
        format: str = "",
        clear_after: bool = False,
        timeout: int = 120,
    ) -> dict[str, Any]:
        return self._run(
            self._browser.generate_batch(
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
        )

    def extract_history(
        self,
        *,
        chat: str = "Meta AI",
        scrolls: int = 30,
        limit: int = 0,
        series: str = "",
        output_dir: str = "",
        format: str = "",
    ) -> dict[str, Any]:
        return self._run(
            self._browser.extract_history(
                chat=chat,
                scrolls=scrolls,
                limit=limit,
                series=series,
                output_dir=output_dir,
                fmt=format,
            )
        )

    def clear_chat(self, chat: str = "Meta AI") -> bool:
        return self._run(self._browser.clear_chat(chat=chat))
