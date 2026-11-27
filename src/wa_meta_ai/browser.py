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


def resolve_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve message direction for every row returned by SCAN_CONVERSATION_JS.

    WhatsApp Web marks only the first message of a consecutive run with a
    ``tail-in`` / ``tail-out`` marker. Continuation rows carry no marker at all,
    so their direction is inherited from the nearest preceding marker. When no
    marker has been seen yet (the visible window starts mid-run) the run is
    resolved from the first marker that does appear, scanning forwards.

    Returns the same rows with ``direction`` set to ``"in"``, ``"out"``, or
    ``"unknown"`` for unresolvable system/notification rows.
    """
    rows = [dict(r) for r in raw_rows]

    # First pass: an explicit marker always wins.
    for row in rows:
        if row.get("tailOut"):
            row["direction"] = "out"
        elif row.get("tailIn"):
            row["direction"] = "in"

    # Second pass: backfill rows before the first marker from the marker that
    # follows them, so a window that opens mid-run is still attributed correctly.
    first_marker = next((i for i, r in enumerate(rows) if r.get("direction")), None)
    if first_marker is not None:
        lead_direction = rows[first_marker]["direction"]
        for i in range(first_marker):
            rows[i]["direction"] = lead_direction

    # Third pass: continuation rows inherit from the previous resolved row.
    current = None
    for row in rows:
        if row.get("direction"):
            current = row["direction"]
        elif row.get("system"):
            row["direction"] = "unknown"
        else:
            row["direction"] = current or "unknown"

    return rows


def incoming_rows(resolved_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows Meta AI sent: incoming direction, excluding notifications/system rows."""
    return [
        r for r in resolved_rows
        if r.get("direction") == "in" and not r.get("system")
    ]

# Scans the conversation and returns one record per message row, describing the
# row factually. Direction is deliberately NOT resolved here: WhatsApp Web marks
# only the first message of a consecutive run with a tail-in/tail-out marker, so
# continuation rows are ambiguous in isolation and must be resolved in order
# against their neighbours. See resolve_rows() below.
SCAN_CONVERSATION_JS = """
(opts) => {
    const minSize = (opts && opts.minSize) || 120;
    const main = document.querySelector('#main');
    if (!main) return { present: false, rows: [] };
    const scope = main.querySelector('[data-testid="conversation-panel-messages"]') || main;

    const isMediaSrc = (s, alt) => {
        if (s.startsWith('blob:')) return true;
        if (!s.startsWith('https://')) return false;
        if (s.includes('static.whatsapp.net') || s.includes('/t61.24694-24/')) return false;
        if (alt === 'Meta AI generated image') return true;
        return s.includes('mmg.whatsapp.net') || s.includes('cdn.whatsapp.net') || s.includes('fna.whatsapp.net');
    };

    const rows = [];
    for (const row of scope.querySelectorAll('[role="row"]')) {
        const holder = row.querySelector('[data-testid^="conv-msg-"]');
        if (!holder) continue;

        const id = (holder.getAttribute('data-testid') || '').replace('conv-msg-', '');
        if (!id) continue;

        const icons = [...row.querySelectorAll('[data-icon]')]
            .map(e => e.getAttribute('data-icon') || '')
            .filter(Boolean);

        const text = [...row.querySelectorAll('[data-testid="selectable-text"], span.selectable-text, .selectable-text')]
            .map(s => (s.innerText || '').trim())
            .filter(Boolean)
            .join('\\n')
            .trim();

        const media = [];
        for (const el of row.querySelectorAll('img, video')) {
            const isVideo = el.tagName === 'VIDEO';
            const src = isVideo
                ? (el.currentSrc || el.src || (el.querySelector('source') && el.querySelector('source').src) || '')
                : (el.currentSrc || el.src || '');
            if (!src) continue;
            const alt = el.getAttribute('alt') || '';
            if (!isMediaSrc(src, alt)) continue;
            const rect = el.getBoundingClientRect();
            const w = isVideo ? (el.videoWidth || rect.width) : (el.naturalWidth || rect.width);
            const h = isVideo ? (el.videoHeight || rect.height) : (el.naturalHeight || rect.height);
            if (w < minSize || h < minSize) continue;
            media.push({
                kind: isVideo ? 'video' : 'image',
                src,
                w, h,
                complete: isVideo ? true : Boolean(el.complete),
            });
        }

        rows.push({
            id,
            tailIn: Boolean(row.querySelector('[data-testid="tail-in"]')),
            tailOut: Boolean(row.querySelector('[data-testid="tail-out"]')),
            youLabel: Boolean(row.querySelector('span[aria-label="You:"], [aria-label="You:"]')),
            system: Boolean(row.querySelector('[data-testid="msg-notification-container"], [data-testid="system_message"]')),
            icons,
            text,
            media,
        });
    }
    return { present: true, rows };
}
"""


CREATING_HINTS = (
    "creating your image",
    "generating",
    "drawing",
    "one moment",
    "please wait",
)


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
        """Return an active WhatsApp Web page, recovering automatically if the tab closed."""
        if self._context is None:
            await self.start()
        assert self._context is not None

        if self._page is None or self._page.is_closed():
            for p in self._context.pages:
                if not p.is_closed() and "web.whatsapp.com" in p.url:
                    self._page = p
                    return self._page
            try:
                self._page = await self._context.new_page()
                await self._page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
                await self._page.wait_for_selector(
                    "#pane-side, #main, canvas[aria-label*='QR'], [data-ref]", timeout=12_000
                )
            except Exception:
                await self.stop()
                await self.start()
        assert self._page is not None
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

    async def _wait_for_app_shell(self, page: Page, timeout: float = 30.0) -> bool:
        """Wait for WhatsApp Web to render its app shell.

        The chat list is populated asynchronously after load, so a single
        immediate query reports DISCONNECTED on a perfectly valid session.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await page.locator("#pane-side, #main").count() > 0:
                return True
            if await page.locator("canvas[aria-label*='QR'], [data-ref]").count() > 0:
                return True
            await asyncio.sleep(0.5)
        return False

    async def auth_status(self) -> str:
        """Return READY, NEEDS_QR, or DISCONNECTED (auto-dismissing modals & 'Use here')."""
        page = await self._require_page()
        use_here = page.locator('button:has-text("Use here")').first
        if await use_here.count() > 0 and await use_here.is_visible():
            await use_here.click()
            await page.wait_for_timeout(1200)

        await self._wait_for_app_shell(page)

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
            if not await self._open_chat_ready(chat):
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

    async def _open_chat_ready(self, chat: str) -> bool:
        """Open a chat and wait until the conversation pane is actually mounted."""
        page = await self._require_page()
        # The chat list renders asynchronously; searching before it mounts
        # silently fails on an otherwise healthy session.
        await self._wait_for_app_shell(page)
        if not await self.open_chat(chat):
            return False
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if await page.locator(
                '[data-testid="conversation-panel-messages"]'
            ).count() > 0:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _type_into_composer(self, page: Page, text: str) -> None:
        """Type into the message composer, which doubles as the caption field."""
        box = page.locator(
            '#main footer [contenteditable="true"][role="textbox"], '
            '#main footer [data-testid="conversation-compose-box-input"], '
            '#main footer [contenteditable="true"]'
        ).first
        await box.wait_for(state="visible", timeout=15_000)
        await box.click()
        await box.fill(text)
        await page.wait_for_timeout(250)

    async def _press_send(self, page: Page) -> bool:
        """Press the send control (attachment send button, or Enter)."""
        for sel in (
            'button[aria-label^="Send"]',
            '[role="button"][aria-label^="Send"]',
            '#main footer button[aria-label^="Send"]',
        ):
            btn = page.locator(sel).last
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(500)
                return True
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        return True

    async def _send_message(self, page: Page, message: str, ref_image: str = "") -> None:
        """Send a message, attaching ``ref_image`` as part of the *same* message.

        When a reference image is supplied the caption is typed into the message
        composer while the attachment is staged, so WhatsApp sends one message
        containing both. Meta AI reads the image and prompt together; sending
        them as two messages makes it start working on the bare image first and
        the prompt arrives too late. There is deliberately no fallback that
        sends them separately.
        """
        if not ref_image:
            await self._type_into_composer(page, message)
            await self._press_send(page)
            return

        img_path = Path(ref_image).expanduser().resolve()
        if not img_path.is_file():
            raise RuntimeError(f"Reference image not found: {img_path}")

        # Stage the attachment. The footer file input accepts images and does
        # not require opening the attachment menu.
        file_input = page.locator('#main footer input[type="file"]').first
        if await file_input.count() > 0:
            await file_input.set_input_files(str(img_path))
        else:
            attach_btn = page.locator(
                '#main footer button[aria-label="Attach"], #main footer [data-icon="plus"]'
            ).first
            await attach_btn.click()
            photos_item = page.locator(
                'button[aria-label="Photos & videos"], [role="menuitem"]:has-text("Photos")'
            ).first
            await photos_item.wait_for(state="visible", timeout=8000)
            try:
                fallback_input = page.locator('input[type="file"][accept*="image"]').first
                await fallback_input.set_input_files(str(img_path))
            except Exception:
                async with page.expect_file_chooser() as fc_info:
                    await photos_item.click()
                chooser = await fc_info.value
                await chooser.set_files(str(img_path))

        # Wait until the attachment is staged (send control or editor appears).
        staged_deadline = time.monotonic() + 20.0
        while time.monotonic() < staged_deadline:
            if await page.locator(
                'button[aria-label^="Send"], [data-testid="media-editor-canvas"], '
                '[data-testid="media-editor-attachment-thumb"]'
            ).count() > 0:
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Attachment did not stage in time; not sending anything")

        # Type the prompt while the attachment is staged so both go as one message.
        await self._type_into_composer(page, message)
        await page.wait_for_timeout(500)

        typed = (await page.locator(
            '#main footer [contenteditable="true"]'
        ).first.inner_text()).strip()
        if message.strip() and message.strip()[:15] not in typed:
            # Caption did not land in the staging composer; abort rather than
            # sending an image-only message followed by a separate prompt.
            raise RuntimeError(
                "Could not attach the prompt to the reference image; "
                "aborted instead of sending image and text as separate messages"
            )

        await self._press_send(page)

    async def _scan_conversation(self, page: Page, min_size: int = 120) -> list[dict[str, Any]]:
        """Scan the open chat and return direction-resolved message rows."""
        deadline = time.monotonic() + 15.0
        raw: dict[str, Any] = {"present": False, "rows": []}
        while time.monotonic() < deadline:
            raw = await page.evaluate(SCAN_CONVERSATION_JS, {"minSize": min_size})
            if raw.get("present"):
                return resolve_rows(raw.get("rows", []))
            await asyncio.sleep(0.5)
        raise RuntimeError(
            "The chat pane is not open; call open_chat() first "
            f"(waited {int(15)}s for #main)"
        )

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
        saved_grace: float = 6.0,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Send one message, wait for the reply to settle, and save newly arrived media.

        Media is attributed by message-row identity: every row present before the
        prompt is recorded, and only media inside rows that did not exist before
        the send is saved. This is immune to the ordering problems of diffing
        image URLs, where a still-rendering image has no stable URL yet and the
        previous image is re-saved instead.
        """
        since = time.monotonic() - self._last_prompt_ts
        wait_target = self.settings.cooldown_sec + random.uniform(0.0, self.settings.jitter_sec)
        if self._last_prompt_ts and since < wait_target:
            await asyncio.sleep(wait_target - since)

        t0 = time.monotonic()
        before_rows = await self._scan_conversation(page)
        before_ids = {r["id"] for r in before_rows}

        await self._send_message(page, message, ref_image=ref_image)
        self._last_prompt_ts = time.monotonic()

        new_rows: list[dict[str, Any]] = []
        reply_text = ""
        last_signature: tuple[Any, ...] | None = None
        stable_since: float | None = None

        while time.monotonic() - t0 < timeout:
            await asyncio.sleep(0.8)
            rows = await self._scan_conversation(page)
            new_rows = [r for r in rows if r["id"] not in before_ids]
            incoming = [r for r in new_rows if r["direction"] == "in" and not r.get("system")]
            if not incoming:
                continue

            reply_text = incoming[-1]["text"]
            generating = any(h in reply_text.lower() for h in CREATING_HINTS)
            media = [m for r in incoming for m in r["media"]]
            signature = (
                tuple(r["id"] for r in new_rows),
                len(media),
                tuple(bool(m["complete"]) for m in media),
                generating,
            )

            if signature == last_signature:
                if stable_since is None:
                    stable_since = time.monotonic()
            else:
                last_signature = signature
                stable_since = None

            settled = (
                not generating
                and bool(media)
                and all(m["complete"] for m in media)
                and stable_since is not None
                and (time.monotonic() - stable_since) >= settle_sec
            )
            if settled:
                break

        # Meta AI often delivers a second image several seconds after the first
        # reply, which previously landed after the settle window and was then
        # misattributed to the *next* prompt. After the first reply settles,
        # watch for a short grace period so late extras are attributed to the
        # prompt that caused them.
        if saved_grace > 0 and new_rows:
            grace_deadline = time.monotonic() + saved_grace
            while time.monotonic() < grace_deadline:
                await asyncio.sleep(0.8)
                rows = await self._scan_conversation(page)
                late = [r for r in rows if r["id"] not in before_ids]
                if len(late) != len(new_rows):
                    extra = [r for r in late if r["id"] not in {x["id"] for x in new_rows}]
                    new_rows.extend(extra)

        incoming = [r for r in new_rows if r["direction"] == "in" and not r.get("system")]
        reply_text = incoming[-1]["text"] if incoming else ""

        saved_media: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in incoming:
            for item in row["media"]:
                if item["src"] in seen:
                    continue
                seen.add(item["src"])
                is_new, rec = await self._save_item(
                    page, item, store, target_fmt, prompt_label, chat, name=name
                )
                if is_new:
                    saved_media.append(rec)
        return reply_text, saved_media

    async def generate(
        self,
        prompt: str,
        chat: str = "Meta AI",
        timeout: int = 90,
        settle_sec: float = 3.0,
        late_grace: float = 6.0,
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
            if not await self._open_chat_ready(chat):
                raise RuntimeError(f"Could not find chat '{chat}'")

            t0 = time.monotonic()
            store = MediaStore(base_dir, series=series, save_manifest=self.settings.save_manifest)
            reply_text, all_media = await self._send_and_collect(
                page, prompt, chat, timeout, settle_sec, store, target_fmt, prompt,
                ref_image=ref_image, name=name, saved_grace=late_grace,
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
                    page, continue_text, chat, timeout, settle_sec, store, target_fmt, prompt,
                    name=name, saved_grace=late_grace,
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
            if not await self._open_chat_ready(chat):
                raise RuntimeError(f"Could not find chat '{chat}'")

            store = MediaStore(base_dir, series=series, save_manifest=self.settings.save_manifest)
            seen_urls: set[str] = set()
            saved: list[dict[str, Any]] = []
            duplicates = 0

            for step in range(scrolls + 1):
                rows = await self._scan_conversation(page, min_size=min_size)
                items = [m for r in rows for m in r["media"]]
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
