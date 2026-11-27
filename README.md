# WhatsApp Meta AI (`wa-meta-ai`)

<p align="left">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/FastAPI-REST%20API-009688" alt="FastAPI">
  <img src="https://img.shields.io/badge/MCP-Compatible-purple" alt="MCP">
  <img src="https://img.shields.io/badge/Playwright-Chromium-orange" alt="Playwright">
</p>

An open-source **REST API, CLI, Python SDK, and MCP server** for two-way interaction with **Meta AI on WhatsApp Web** — send text or reference-image prompts, auto-continue batched replies (`15+` images), convert `.webp` → `.png`/`.jpg`, save clean images directly into your project folder, and clear chat history between series using your own WhatsApp account.

> **Disclaimer:** Uses Playwright to automate official WhatsApp Web in Chromium. Use responsibly with built-in cooldown/jitter (`WA_COOLDOWN_SEC`, `WA_JITTER_SEC`) and preferably on a secondary WhatsApp number.

---

## Why This Exists

- WhatsApp Web **does not index** Meta AI generated images into *"Media, links, and docs"* (`0` items) and offers no bulk-select/download UI.
- Meta AI images are delivered inline as full-resolution (`1600×1600` / `1920×1280` / `1184×2096`) `mmg.whatsapp.net/..._n.enc` URLs with `Content-Type: application/octet-stream` — even though the payload is plain browser-decodable WebP.
- `wa-meta-ai` automates the entire loop cleanly: **prompt (with optional reference image) → settle detection → magic-byte sniffing → PNG/JPG conversion → clean deduplicated output**.

---

## Features

- **Two-Way Prompting (`POST /generate`):** Send a prompt (plus optional `ref_image`) to Meta AI, wait for all generated images to settle, and download full-resolution files.
- **Reference Image (`ref_image`):** Attach a local photo (`Photos & videos`) along with your prompt for consistent character/style variations.
- **Auto-Continue Batches (`target_count`):** Automatically sends `"continue"` across batched Meta AI replies until `target_count` unique images are saved.
- **Batch Series Runner (`POST /generate/batch`):** Run a list of scene prompts into `series` subfolders with optional `clear_after: true`.
- **Clean Output (`output_dir`):** Save directly into any project folder with **zero metadata clutter** by default (`WA_SAVE_MANIFEST=false`) and SHA-256 filename deduplication.
- **Async Jobs & Webhooks:** Pass `?async=true` (or `async_mode: true`) for `202 Accepted` + `job_id`, poll `GET /jobs/{job_id}`, or receive `webhook_url` POST callbacks.
- **OpenAI-Compatible Endpoint (`POST /v1/images/generations`):** Drop-in backend for tools using OpenAI's `images.generate()` (`url` or `b64_json`).
- **Built-in MCP Server (`wa-meta-ai mcp`):** Exposes `auth_status`, `generate_images`, `generate_batch`, `extract_history`, `clear_chat`, and `list_media` to AI agents (Antigravity, Claude Desktop, Cursor, etc.).
- **Self-Serve QR Dashboard (`GET /`):** Live auto-refreshing QR setup page + Swagger docs at `/docs`.

---

## Quick Start

### 1. Install
```bash
pip install git+https://github.com/adityanshinde/WhatsApp-Meta-AI.git
playwright install chromium
```
*(Or clone locally and run `pip install -e .`)*

### 2. Start the Server
```bash
wa-meta-ai serve
```
- Open **`http://127.0.0.1:8000/`** (or the opened Chrome window) and scan the WhatsApp Web QR code once.
- Your session persists in `wa_profile/` (local `./wa_profile` if present, otherwise `~/.wa-meta-ai/profile`).
- Interactive API docs: **`http://127.0.0.1:8000/docs`**

---

## Usage Examples

### REST API (`POST /generate`)
```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Place this same person in a sunlit European cafe wearing a beige trench coat",
    "ref_image": "portrait.png",
    "output_dir": "./assets/cafe_series",
    "format": "png",
    "target_count": 2
  }'
```

### CLI
```bash
wa-meta-ai generate "Generate 4 cyberpunk street scenes" --series cyberpunk --format png
wa-meta-ai generate "Neon rain portrait" --ref portrait.png --target-count 2
wa-meta-ai extract --chat "Meta AI" --scrolls 30 --format png
wa-meta-ai clear
```

### OpenAI Python SDK Drop-In
```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
res = client.images.generate(prompt="Cute futuristic robot", n=2)
print(res.data[0].url)
```

### MCP Server (`mcp_config.json`)
Add to Antigravity / Claude Desktop / Cursor:
```json
{
  "mcpServers": {
    "wa-meta-ai": {
      "command": "wa-meta-ai",
      "args": ["mcp"]
    }
  }
}
```
If `wa-meta-ai serve` is already running, `wa-meta-ai mcp` automatically proxies calls through it so Chrome's profile is never locked by two processes.

### Optional: Docker (Headless Linux / VPS)
```bash
docker compose up --build -d
# Open http://127.0.0.1:8000/ to scan the WhatsApp QR code
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Self-serve QR setup & status dashboard |
| `GET` | `/health` | Service & WhatsApp auth status |
| `GET` | `/auth/status` | `READY` \| `NEEDS_QR` \| `DISCONNECTED` |
| `GET` | `/auth/qr` | Live PNG screenshot of WhatsApp Web QR code |
| `POST` | `/generate` | Send prompt (+ optional `ref_image`), wait & download media |
| `POST` | `/generate/batch` | Run a list of prompts sequentially (`clear_after` supported) |
| `GET` | `/jobs` / `/jobs/{id}` | List or poll async generation jobs |
| `POST` | `/extract` | Scroll chat history upward & download inline media |
| `POST` | `/chat/clear` | Clear Meta AI chat between series |
| `GET` | `/media` / `/media/{file}` | List or fetch saved media |
| `POST` | `/v1/images/generations` | OpenAI-compatible image generation endpoint |

---

## Configuration (`.env`)

Copy [`.env.example`](.env.example) to `.env` — loaded automatically by `python-dotenv`:

| Variable | Default | Description |
|---|---|---|
| `WA_HOST` | `127.0.0.1` | Server bind host |
| `WA_PORT` | `8000` | Server port |
| `WA_DOWNLOADS_DIR` | `~/Downloads/wa-meta-ai` | Default output folder when `output_dir` isn't passed |
| `WA_SAVE_MANIFEST` | `false` | Write `manifest.json` alongside images (`false` = images only) |
| `WA_DEFAULT_FORMAT` | `png` | Output image format (`png`, `jpg`, `webp`, `original`) |
| `WA_COOLDOWN_SEC` | `2.0` | Minimum delay between prompts |
| `WA_JITTER_SEC` | `1.5` | Random extra delay (`0..WA_JITTER_SEC`) |
| `WA_META_AI_API_KEY` | `""` | Optional `X-API-Key` / `Bearer` token |
| `WA_HEADLESS` | `false` | Run Chromium headless |

---

## License

[MIT](LICENSE)
