# WhatsApp Meta AI API — Technical Specification (`SPEC.md`)

An open-source **Python SDK, CLI, and FastAPI REST API** for two-way interaction with **Meta AI** on WhatsApp Web — send prompts, wait for generated images/videos/text, and automatically extract & organize all media using your own WhatsApp account (no WhatsApp Business API required).

---

## 1. Key Technical Findings (WhatsApp Web Internals)

- **Why "Media, links, and docs" doesn't work for Meta AI:** WhatsApp Web does not index Meta AI generated images into the chat's Media gallery (`0` items shown even when images exist in chat).
- **How Meta AI images are delivered:** Unlike standard E2EE user-to-user media (which are fetched via XHR, decrypted in JS, and rendered as `blob:` URLs), Meta AI images are loaded directly via `<img src="https://mmg.whatsapp.net/v/t62.7118-24/..._n.enc?..." alt="Meta AI generated image">` at full resolution (e.g., `1920×1280`).
- **`.enc` extension & `Content-Type` quirk:** Despite the `.enc` path suffix and `Content-Type: application/octet-stream` response header, the payload is unencrypted browser-decodable media (typically `RIFF....WEBP` or JPEG/PNG). File extensions must be determined by **magic-byte sniffing**, not headers or URL suffixes.
- **Direct downloadability:** Signed `mmg.whatsapp.net` URLs can be downloaded directly via standard HTTP (`urllib` / `httpx`) without cookies, though the signed query parameters (`oh`, `oe`) expire over time — so extraction should happen immediately after generation or during a live scroll session.

---

## 2. Goals & Non-Goals

### Goals
- **One-command setup:** `pip install wa-meta-ai` (or `docker compose up`) with persistent QR login (`wa_profile/`).
- **Two-way prompt → media pipeline:** Send a prompt to Meta AI, detect when generation completes, download all resulting images/videos, and return structured JSON + local file paths.
- **Bulk historical extraction:** Scroll any chat (`Meta AI` or group chats using `@Meta AI`) and extract all `mmg.whatsapp.net` and `blob:` media with SHA-256 deduplication.
- **Drop-in compatibility:** Optional OpenAI-compatible `/v1/images/generations` endpoint so existing apps/agents can use Meta AI as an image backend.

### Non-Goals
- Reverse-engineering WhatsApp's encrypted WebSocket protocol (Baileys-style). Using real Chromium via Playwright keeps behavior aligned with official WhatsApp Web and avoids protocol breakage.

---

## 3. Architecture & Package Layout

```text
Whatsapp Meta AI/
├── src/wa_meta_ai/
│   ├── __init__.py       # Public Python SDK (MetaAIClient)
│   ├── config.py         # Settings via env vars / .env (port, profile, timeouts, API key)
│   ├── browser.py        # Playwright persistent context + CDP attach + prompt/completion loop
│   ├── client.py         # Synchronous Python SDK (MetaAIClient)
│   ├── extractor.py      # DOM/network media finder, magic-byte sniffer, Pillow converter, SHA-256 store
│   ├── jobs.py           # Async job queue + optional webhook dispatcher
│   ├── server.py         # FastAPI app (REST + QR UI + OpenAI-compat endpoint)
│   ├── mcp_server.py     # FastMCP server for AI agents (wa-meta-ai mcp)
│   └── cli.py            # CLI (`wa-meta-ai serve|generate|extract|clear|mcp`)
├── downloads/            # Extracted media + manifest.json
├── wa_profile/           # Persistent Chromium user-data-dir (gitignored)
├── tests/
├── Dockerfile
├── pyproject.toml
├── SPEC.md
└── README.md
```

---

## 4. Additional Features to Include

1. **Async Job Queue + Webhooks (`POST /generate?async=true`)**
   - Image/video generation takes ~5–25s. Support both synchronous wait and `202 Accepted` with `job_id` (`GET /jobs/{id}`) + optional `webhook_url` callback when done.
2. **Format Conversion (`format=webp|png|jpg`)**
   - Meta AI defaults to `.webp`. Optional Pillow conversion lets callers request `.png` or `.jpg` directly in the API/CLI.
3. **Prompt Metadata & Searchable Manifest**
   - Store `{ filename, sha256, prompt, chat, width, height, mime, created_at, source_url }` in `downloads/manifest.json` (and optional `.json` sidecars) so media can be queried by prompt or date (`GET /media?q=cyberpunk`).
4. **OpenAI-Compatible Image Endpoint (`POST /v1/images/generations`)**
   - Accepts `{ "prompt": "...", "n": 1, "response_format": "url" | "b64_json" }` so tools like OpenWebUI, LangChain, or custom scripts work out of the box.
5. **Headless QR Auth (`GET /auth/qr` & Terminal ASCII QR)**
   - When running headless or in Docker, expose the live WhatsApp Web QR code as a PNG endpoint and print an ASCII QR in the terminal.
6. **API Key Security & Rate-Limit Guard**
   - Optional `WA_META_AI_API_KEY` (`Authorization: Bearer ...` or `X-API-Key`).
   - Built-in request serialization (`asyncio.Lock`) + configurable cooldown (`--cooldown 2.0`) to prevent overlapping prompts or WhatsApp spam flags.
7. **Multi-Chat & `@Meta AI` Group Support**
   - Default target is `"Meta AI"`, with optional `chat` parameter to extract from or prompt inside any WhatsApp chat/group.
- **Video / Animation:** Empirically tested on WhatsApp Web — Meta AI replies *"Video / GIF nahi bana sakta"* and falls back to generating a still `1600×1600` image. The API therefore focuses on **images + text** (while still supporting `blob:`/`<video>` capture in bulk chat extraction).

---

## 5. REST API Contract

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service & browser liveness check |
| `GET` | `/auth/status` | Returns `{ "status": "READY" \| "NEEDS_QR" \| "DISCONNECTED" }` |
| `GET` | `/auth/qr` | Returns PNG screenshot of current WhatsApp Web QR code |
| `POST` | `/generate` | Send prompt to Meta AI, wait for reply + media, save & return metadata |
| `POST` | `/chat/clear` | Clear the Meta AI chat (`Menu -> Clear chat -> Confirm`) for a fresh series |
| `GET` | `/jobs/{job_id}` | Poll status/result of an async generation job |
| `POST` | `/extract` | Scroll a chat (`chat`, `scrolls`, `limit`) and extract historical media |
| `GET` | `/media` | List saved media (supports `?q=` prompt search and pagination) |
| `GET` | `/media/{filename}` | Download/serve a specific extracted file |
| `POST` | `/v1/images/generations` | OpenAI-compatible image generation endpoint |

### Example: `POST /generate`
**Request:**
```json
{
  "prompt": "Generate a minimalist 3D render of a glowing glass cube on dark slate",
  "chat": "Meta AI",
  "format": "png",
  "timeout": 90,
  "webhook_url": null
}
```

**Response (`200 OK`):**
```json
{
  "job_id": "job_8f3a1c",
  "prompt": "Generate a minimalist 3D render of a glowing glass cube on dark slate",
  "text": "Here's an image of a glowing glass cube on dark slate.",
  "media": [
    {
      "filename": "meta_ai_0002_a1b2c3d4.png",
      "url": "/media/meta_ai_0002_a1b2c3d4.png",
      "width": 1920,
      "height": 1280,
      "bytes": 1420512,
      "sha256": "a1b2c3d4..."
    }
  ],
  "elapsed_seconds": 11.4
}
```

---

## 6. Python SDK Example

```python
from wa_meta_ai import MetaAIClient

with MetaAIClient(profile_dir="./wa_profile") as client:
    client.wait_for_login()
    result = client.generate("Futuristic Tokyo street in rain, anime style", format="png")
    for item in result["media"]:
        print(item["path"], item["width"], item["height"])
```

---

## 7. Implementation Roadmap

- **Phase 1 — Core Package & API (MVP):**
  - `browser.py` (persistent profile + CDP attach + `asyncio.Lock`)
  - `extractor.py` (`mmg.whatsapp.net` + `blob:` + magic-byte sniffer + SHA-256 manifest)
  - `prompt.py` (send prompt, detect new image/text response)
  - `server.py` (`/health`, `/auth/status`, `/auth/qr`, `/generate`, `/extract`, `/media`)
  - `cli.py` + `pyproject.toml`
- **Phase 2 — Production & DX Features:**
  - Async `jobs.py` + `webhook_url`
  - Pillow format conversion (`webp` → `png`/`jpg`)
  - Prompt metadata search (`GET /media?q=`)
  - API key middleware + cooldown/jitter
- **Phase 3 — Ecosystem & Open-Source Release:**
  - `POST /v1/images/generations` (OpenAI-compatible)
  - `Dockerfile` + `docker-compose.yml` (headless Chromium)
  - Unit & integration tests + MIT License + GitHub Actions CI
