"""Playwright-backed WhatsApp Web session manager for Meta AI interaction."""

from __future__ import annotations

import asyncio
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import Settings
from .extractor import MediaStore, download_url_bytes, extract_blob_bytes

COLLECT_CHAT_MEDIA_JS = """
(opts) => {
    const minSize = (opts && opts.minSize) || 120;
    const incomingOnly = Boolean(opts && opts.incomingOnly);
    const out = [];
    const seen = new Set();
    const root = document.querySelector('#main') || document;
    const isMediaSrc = (s, alt) => {
        if (s.startsWith('blob:')) return true;
        if (!s.startsWith('https://')) return false;
        if (s.includes('static.whatsapp.net') || s.includes('/t61.24694-24/')) return false;
        if (alt === 'Meta AI generated image') return true;
        return s.includes('mmg.whatsapp.net') || s.includes('cdn.whatsapp.net') || s.includes('fna.whatsapp.net');
    };
    for (const el of root.querySelectorAll('img, video')) {
        if (incomingOnly && (el.closest('.message-out') || el.closest('[data-id^="true_"]'))) continue;
        const isVideo = el.tagName === 'VIDEO';
        const src = isVideo
            ? (el.currentSrc || el.src || (el.querySelector('source') && el.querySelector('source').src) || '')
            : (el.currentSrc || el.src || '');
        if (incomingOnly && src.startsWith('blob:')) continue;
        const alt = el.getAttribute('alt') || '';
        if (!isMediaSrc(src, alt) || seen.has(src)) continue;
        const r = el.getBoundingClientRect();
        const nw = isVideo ? (el.videoWidth || r.width) : (el.naturalWidth || r.width);
        const nh = isVideo ? (el.videoHeight || r.height) : (el.naturalHeight || r.height);
        const complete = isVideo ? true : Boolean(el.complete);
        if (nw >= minSize && nh >= minSize) {
            seen.add(src);
            out.push({ kind: isVideo ? 'video' : 'image', src, w: nw, h: nh, complete });
        }
    }
    return out;
}
"""

LAST_INCOMING_TEXT_JS = """
() => {
    const main = document.querySelector('#main');
    if (!main) return '';
    const msgs = [...main.querySelectorAll('.message-in, [data-id^="false_"]')];
    if (!msgs.length) return '';
    const last = msgs[msgs.length - 1];
    const spans = [...last.querySelectorAll('span.selectable-text')];
    return spans.map(s => s.innerText).join('\\n').trim();
}
"""

COUNT_INCOMING_MSGS_JS = """
() => {
    const main = document.querySelector('#main');
    return main ? main.querySelectorAll('.message-in, [data-id^="false_"]').length : 0;
}
"""


class WhatsAppBrowser:
    """Manages a single serialized Playwright WhatsApp Web page."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Any = None
        self._browser: Any = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._last_prompt_ts = 0.0

    async def start(self) -> None:
        """Launch or attach to Chromium and open WhatsApp Web."""
        self._pw = await async_playwright().start()
        if self.settings.cdp_url:
            self._browser = await self._pw.chromium.connect_over_cdp(self.settings.cdp_url)
            self._context = self._browser.contexts[0]
        else:
            self.settings.profile_dir.mkdir(parents=True, exist_ok=True)
            launch_opts: dict[str, Any] = {
                "user_data_dir": str(self.settings.profile_dir),
                "headless": self.settings.headless,
                "args": ["--disable-notifications"] + ([] if self.settings.headless else ["--start-maximized"]),
            }
            if self.settings.headless:
                launch_opts["viewport"] = {"width": 1400, "height": 900}
            else:
                launch_opts["no_viewport"] = True
            try:
                self._context = await self._pw.chromium.launch_persistent_context(
                    channel="chrome", **launch_opts
                )
            except Exception:
                try:
                    self._context = await self._pw.chromium.launch_persistent_context(**launch_opts)
                except Exception as exc:
                    if "Executable doesn't exist" in str(exc):
                        await asyncio.to_thread(
                            subprocess.check_call,
                            [sys.executable, "-m", "playwright", "install", "chromium"],
                        )
                        self._context = await self._pw.chromium.launch_persistent_context(**launch_opts)
                    else:
                        raise

        assert self._context is not None
        for p in self._context.pages:
            if "web.whatsapp.com" in p.url:
                self._page = p
                break
        if self._page is None:
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            await self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
            try:
                await self._page.wait_for_selector(
                    "#pane-side, #main, canvas[aria-label*='QR'], [data-ref]", timeout=12_000
                )
            except Exception:
                pass

    async def stop(self) -> None:
        """Close Playwright resources cleanly."""
        if self._context and not self.settings.cdp_url:
            await self._context.close()
        if self._pw:
            await self._pw.stop()

    async def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started")
        return self._page

    async def debug_ui(self) -> dict[str, Any]:
        """Return visible chat titles, dialogs, and search inputs for diagnostics."""
        page = await self._require_page()
        return await page.evaluate(
            """
            () => ({
                url: location.href,
                dialogs: [...document.querySelectorAll('[role="dialog"]')].map(d => d.innerText.slice(0, 120)),
                titles: [...document.querySelectorAll('#pane-side span[title]')].slice(0, 15).map(s => s.getAttribute('title')),
                buttons: [...document.querySelectorAll('button[aria-label]')].slice(0, 20).map(b => b.getAttribute('aria-label')),
                inputs: [...document.querySelectorAll('[contenteditable="true"], input')].map(el => ({
                    tag: el.tagName, role: el.getAttribute('role'), aria: el.getAttribute('aria-label'), tab: el.getAttribute('data-tab')
                })),
            })
            """
        )

    async def auth_status(self) -> str:
        """Return READY, NEEDS_QR, or DISCONNECTED (auto-dismissing post-login modals)."""
        page = await self._require_page()
        if await page.locator("#pane-side, #main").count() > 0:
            await self._dismiss_dialogs(page)
            return "READY"
        if await page.locator("canvas[aria-label*='QR'], [data-ref]").count() > 0:
            return "NEEDS_QR"
        return "DISCONNECTED"

    async def qr_screenshot(self) -> bytes | None:
        """Capture PNG screenshot of the WhatsApp Web QR code if present."""
        page = await self._require_page()
        qr = page.locator("canvas[aria-label*='QR'], [data-ref] canvas").first
        if await qr.count() > 0:
            return await qr.screenshot(type="png")
        if await self.auth_status() == "NEEDS_QR":
            return await page.screenshot(type="png")
        return None

    async def _dismiss_dialogs(self, page: Page) -> None:
        """Dismiss any WhatsApp Web onboarding/modal dialogs blocking clicks."""
        for _ in range(3):
            dialog = page.locator('[role="dialog"][aria-modal="true"]').first
            if await dialog.count() == 0:
                return
            for sel in (
                'button:has-text("OK")',
                'button:has-text("Continue")',
                'button:has-text("Got it")',
                'button:has-text("Not now")',
                'button:has-text("Dismiss")',
                'button:has-text("Use here")',
                'button[aria-label="Close"]',
            ):
                btn = dialog.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(500)
                    break
            else:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)

    async def open_chat(self, chat: str = "Meta AI") -> bool:
        """Ensure the target chat is open in #main."""
        page = await self._require_page()
        await self._dismiss_dialogs(page)

        if await page.locator(f'#main header span[title="{chat}"]').count() > 0:
            return True

        target = page.locator(f'#pane-side span[title="{chat}"]').first
        if await target.count() > 0:
            await target.click()
            await page.wait_for_timeout(800)
            return True

        if chat.lower() == "meta ai":
            ai_btn = page.locator('button[aria-label*="Meta AI"], [title="Meta AI"]').first
            if await ai_btn.count() > 0 and await ai_btn.is_visible():
                await ai_btn.click()
                await page.wait_for_timeout(1000)
                if await page.locator('#main footer [contenteditable="true"]').count() > 0:
                    return True

        search = page.locator(
            'input[aria-label="Search or start a new chat"], input[data-tab="3"], #side [role="textbox"], #side [contenteditable="true"]'
        ).first
        if await search.count() > 0:
            await search.click()
            await search.fill(chat)
            await page.wait_for_timeout(1200)
            hit = page.locator(f'#pane-side span[title="{chat}"]').first
            if await hit.count() > 0:
                await hit.click()
                await page.wait_for_timeout(1000)
                return True
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1000)
            return await page.locator(f'#main header span[title="{chat}"]').count() > 0
        return False

    async def clear_chat(self, chat: str = "Meta AI") -> bool:
        """Clear all messages in the target chat using the verified Menu -> Clear chat flow."""
        async with self._lock:
            page = await self._require_page()
            if not await self.open_chat(chat):
                raise RuntimeError(f"Could not open chat '{chat}'")

            menu_btn = page.locator('#main header button[aria-label="Menu"]').first
            await menu_btn.click()
            clear_item = page.locator('[role="menuitem"][aria-label="Clear chat"]').first
            try:
                await clear_item.wait_for(state="visible", timeout=3000)
                if await clear_item.get_attribute("aria-disabled") == "true":
                    await page.keyboard.press("Escape")
                    return True
                await clear_item.click()
            except Exception:
                await page.keyboard.press("Escape")
                return True

            confirm_btn = page.locator('[role="dialog"] button:has-text("Clear chat")').first
            await confirm_btn.wait_for(state="visible", timeout=5000)
            await confirm_btn.click()
            await page.wait_for_timeout(2500)
            return True

    async def _save_item(
        self,
        page: Page,
        item: dict[str, Any],
        store: MediaStore,
        fmt: str,
        prompt: str,
        chat: str,
        name: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        src = item["src"]
        if src.startswith("https://") or src.startswith("http://"):
            raw, ct = await asyncio.to_thread(download_url_bytes, src)
        else:
            raw, ct = await extract_blob_bytes(page, src)
        return store.save_media(
            raw=raw,
            mime=ct,
            kind=item["kind"],
            target_format=fmt,
            prompt=prompt,
            chat=chat,
            source_url=src,
            name=name,
        )

    async def _send_message(self, page: Page, message: str, ref_image: str = "") -> None:
        """Send a text message, optionally attaching a local reference image."""
        if ref_image:
            img_path = Path(ref_image).expanduser().resolve()
            if not img_path.is_file():
                raise RuntimeError(f"Reference image not found: {img_path}")
            attach_btn = page.locator(
                '#main footer button[aria-label="Attach"], #main footer [data-icon="plus"]'
            ).first
            await attach_btn.click()
            photos_item = page.locator(
                'button[aria-label="Photos & videos"], [role="menuitem"]:has-text("Photos")'
            ).first
            await photos_item.wait_for(state="visible", timeout=5000)
            # Prefer Photos & videos input (accepts both image & video); .first image-only input is Sticker!
            file_input = page.locator('input[type="file"][accept*="image"][accept*="video"]').first
            if await file_input.count() > 0:
                await file_input.set_input_files(str(img_path))
            else:
                async with page.expect_file_chooser() as fc_info:
                    await photos_item.click()
                fc = await fc_info.value
                await fc.set_files(str(img_path))

            preview_marker = page.locator('button[aria-label="Crop and rotate"], [aria-label="Send"]').last
            await preview_marker.wait_for(state="visible", timeout=15_000)

            caption_box = page.locator(
                '[aria-label*="caption" i][contenteditable="true"], '
                '[aria-placeholder*="caption" i][contenteditable="true"], '
                '[data-testid="media-caption-input-container"] [contenteditable="true"]'
            ).first
            caption_typed = False
            if await caption_box.count() > 0 and await caption_box.is_visible():
                await caption_box.click()
                await caption_box.fill(message)
                caption_typed = True

            send_btn = page.locator(
                '[aria-label="Send"], [data-icon="send"], [data-icon="wds-ic-send-filled"]'
            ).last
            if await send_btn.count() > 0 and await send_btn.is_visible():
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")

            if message and not caption_typed:
                # Wait for preview overlay to close, then send the prompt as text
                await page.wait_for_timeout(1000)
                box = page.locator('#main footer [contenteditable="true"][role="textbox"]').first
                await box.click()
                await box.fill(message)
                await page.keyboard.press("Enter")
            return

        box = page.locator('#main footer [contenteditable="true"][role="textbox"]').first
        await box.click()
        await box.fill(message)
        await page.keyboard.press("Enter")

    async def _send_and_collect(
        self,
        page: Page,
        message: str,
        chat: str,
        timeout: int,
        settle_sec: float,
        store: MediaStore,
        target_fmt: str,
        prompt_label: str,
        ref_image: str = "",
        name: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Send one message, wait for reply + media settle, and save new media."""
        since = time.monotonic() - self._last_prompt_ts
        wait_target = self.settings.cooldown_sec + random.uniform(0.0, self.settings.jitter_sec)
        if self._last_prompt_ts and since < wait_target:
            await asyncio.sleep(wait_target - since)

        t0 = time.monotonic()
        media_opts = {"minSize": 120, "incomingOnly": True}
        before_urls = {m["src"] for m in await page.evaluate(COLLECT_CHAT_MEDIA_JS, media_opts)}
        before_msgs = await page.evaluate(COUNT_INCOMING_MSGS_JS)

        await self._send_message(page, message, ref_image=ref_image)
        self._last_prompt_ts = time.monotonic()

        new_items: list[dict[str, Any]] = []
        last_msg_count = before_msgs
        last_change = time.monotonic()
        saw_reply = False

        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.8)
            cur_msgs = await page.evaluate(COUNT_INCOMING_MSGS_JS)
            cur_media = await page.evaluate(COLLECT_CHAT_MEDIA_JS, media_opts)
            fresh = [m for m in cur_media if m["src"] not in before_urls]

            wait_window = settle_sec if new_items else max(settle_sec, 7.0)
            if cur_msgs != last_msg_count or len(fresh) != len(new_items):
                saw_reply = True
                last_msg_count = cur_msgs
                new_items = fresh
                last_change = time.monotonic()
            elif saw_reply and (time.monotonic() - last_change) >= wait_window:
                if all(m.get("complete", True) for m in new_items):
                    break

        reply_text = await page.evaluate(LAST_INCOMING_TEXT_JS)
        saved_media: list[dict[str, Any]] = []
        for item in new_items:
            is_new, rec = await self._save_item(page, item, store, target_fmt, prompt_label, chat, name=name)
            if is_new:
                saved_media.append(rec)
        return reply_text, saved_media

    async def generate(
        self,
        prompt: str,
        chat: str = "Meta AI",
        timeout: int = 90,
        settle_sec: float = 3.0,
        fmt: str = "",
        series: str = "",
        output_dir: str = "",
        ref_image: str = "",
        name: str = "",
        target_count: int = 0,
        auto_continue: bool = True,
        continue_text: str = "continue",
        max_rounds: int = 20,
    ) -> dict[str, Any]:
        """Send a prompt to Meta AI, optionally with a reference image and auto-continue."""
        target_fmt = fmt or self.settings.default_format
        base_dir = Path(output_dir).expanduser().resolve() if output_dir else self.settings.downloads_dir
        async with self._lock:
            page = await self._require_page()
            if await self.auth_status() != "READY":
                raise RuntimeError("WhatsApp Web is not logged in (scan QR at /auth/qr)")
            if not await self.open_chat(chat):
                raise RuntimeError(f"Could not find chat '{chat}'")

            t0 = time.monotonic()
            store = MediaStore(base_dir, series=series, save_manifest=self.settings.save_manifest)
            reply_text, all_media = await self._send_and_collect(
                page, prompt, chat, timeout, settle_sec, store, target_fmt, prompt, ref_image=ref_image, name=name
            )
            rounds = 1
            stagnant = 0

            while auto_continue and target_count > 0 and len(all_media) < target_count and rounds < max_rounds:
                # Only continue if Meta AI produced unique media or explicitly asks to continue
                low = reply_text.lower()
                has_cue = any(
                    k in low
                    for k in ("continue", "next", "more images", "remaining", "batch", "chahiye", "aage", "shall i", "want me to")
                )
                if not all_media and not has_cue:
                    break

                rounds += 1
                reply_text, batch = await self._send_and_collect(
                    page, continue_text, chat, timeout, settle_sec, store, target_fmt, prompt, name=name
                )
                if batch:
                    all_media.extend(batch)
                    stagnant = 0
                else:
                    stagnant += 1
                    if stagnant >= 2:
                        break

            return {
                "prompt": prompt,
                "chat": chat,
                "series": store.series,
                "rounds": rounds,
                "text": reply_text,
                "media": all_media,
                "elapsed_seconds": round(time.monotonic() - t0, 2),
            }

    async def generate_batch(
        self,
        prompts: list[str],
        chat: str = "Meta AI",
        series: str = "",
        output_dir: str = "",
        ref_image: str = "",
        names: list[str] | None = None,
        fmt: str = "",
        clear_after: bool = False,
        timeout: int = 90,
        settle_sec: float = 3.0,
    ) -> dict[str, Any]:
        """Run a list of prompts sequentially into a single folder, optionally clearing chat after."""
        t0 = time.monotonic()
        results: list[dict[str, Any]] = []
        all_media: list[dict[str, Any]] = []
        for idx, p in enumerate(prompts):
            item_name = names[idx] if names and idx < len(names) else ""
            res = await self.generate(
                prompt=p,
                chat=chat,
                timeout=timeout,
                settle_sec=settle_sec,
                fmt=fmt,
                series=series,
                output_dir=output_dir,
                ref_image=ref_image if idx == 0 else "",
                name=item_name,
                auto_continue=False,
            )
            results.append({"prompt": p, "text": res["text"], "count": len(res["media"])})
            all_media.extend(res["media"])

        cleared = False
        if clear_after:
            cleared = await self.clear_chat(chat=chat)

        return {
            "series": series,
            "chat": chat,
            "prompts_count": len(prompts),
            "total_media": len(all_media),
            "cleared_after": cleared,
            "media": all_media,
            "results": results,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        }

    async def extract_history(
        self,
        chat: str = "Meta AI",
        scrolls: int = 30,
        limit: int = 0,
        min_size: int = 200,
        fmt: str = "",
        series: str = "",
        output_dir: str = "",
        delay: float = 0.7,
    ) -> dict[str, Any]:
        """Scroll upward through a chat and extract all visible media."""
        target_fmt = fmt or self.settings.default_format
        base_dir = Path(output_dir).expanduser().resolve() if output_dir else self.settings.downloads_dir
        async with self._lock:
            page = await self._require_page()
            if not await self.open_chat(chat):
                raise RuntimeError(f"Could not find chat '{chat}'")

            store = MediaStore(base_dir, series=series, save_manifest=self.settings.save_manifest)
            seen_urls: set[str] = set()
            saved: list[dict[str, Any]] = []
            duplicates = 0

            for step in range(scrolls + 1):
                items = await page.evaluate(COLLECT_CHAT_MEDIA_JS, {"minSize": min_size, "incomingOnly": False})
                for item in items:
                    if item["src"] in seen_urls:
                        continue
                    seen_urls.add(item["src"])
                    src = item["src"]
                    if src.startswith("https://") or src.startswith("http://"):
                        raw, ct = await asyncio.to_thread(download_url_bytes, src)
                    else:
                        raw, ct = await extract_blob_bytes(page, src)
                    is_new, rec = store.save_media(
                        raw=raw,
                        mime=ct,
                        kind=item["kind"],
                        target_format=target_fmt,
                        chat=chat,
                        source_url=src,
                    )
                    if is_new:
                        saved.append(rec)
                    else:
                        duplicates += 1
                    if limit > 0 and len(saved) >= limit:
                        return {"saved": len(saved), "duplicates": duplicates, "media": saved}

                if step == scrolls:
                    break
                await page.evaluate(
                    """
                    () => {
                        const scrollers = [
                            document.querySelector('#main [role="application"]')?.parentElement,
                            document.querySelector('#main .copyable-area > div'),
                            document.querySelector('#main'),
                        ].filter(Boolean);
                        for (const el of scrollers) {
                            if (el.scrollHeight > el.clientHeight) {
                                el.scrollTop = Math.max(0, el.scrollTop - Math.floor(el.clientHeight * 0.85));
                                return;
                            }
                        }
                    }
                    """
                )
                await asyncio.sleep(delay)

            return {"saved": len(saved), "duplicates": duplicates, "media": saved}
