# WhatsApp Meta AI API (`wa-meta-ai`)

Open-source **REST API, Python SDK, and CLI** for two-way interaction with **Meta AI** on WhatsApp Web — send prompts, auto-continue batched replies (`15+` images), convert `.webp` → `.png`/`.jpg`, organize outputs by series, and clear chat history between runs using your own WhatsApp account.

> **Disclaimer:** Uses Playwright to automate WhatsApp Web in Chromium. Use responsibly with reasonable cooldowns (`WA_COOLDOWN_SEC`, `WA_JITTER_SEC`) and preferably on a secondary number.

---

## Features

- **Two-Way Prompting (`POST /generate`):** Sends prompts to Meta AI, waits for all generated images to settle, and downloads full-resolution files (`1600×1600` / `1920×1280`).
- **Auto-Continue Batches:** Set `target_count` (e.g. `15` or `80`) and `auto_continue: true` to automatically reply `"continue"` across batched responses.
- **Batch Series Runner (`POST /generate/batch`):** Run a list of scene prompts into `downloads/<series>/` with optional `clear_after: true`.
- **Async Jobs & Webhooks:** Use `?async=true` (or `async_mode: true`) to get `202 Accepted` + `job_id`, poll `GET /jobs/{job_id}`, or receive `webhook_url` callbacks.
- **OpenAI-Compatible Endpoint (`POST /v1/images/generations`):** Drop-in replacement for tools expecting OpenAI's image API (`url` or `b64_json`).
- **Automatic Magic-Byte Sniffing & Conversion:** Detects `.webp`/`.jpg`/`.png` from bytes (ignoring WhatsApp's `application/octet-stream` `.enc` header) and converts to `png` (default), `jpg`, or `webp`.
- **Chat Cleanup (`POST /chat/clear`):** Clears the Meta AI chat between series via `Menu -> Clear chat`.
- **Headless QR Endpoint (`GET /auth/qr`):** Scan WhatsApp Web QR remotely when running headless or in Docker.

---

## Quick Start

### 1. Install
```bash
pip install -e .
playwright install chromium
```

### 2. Start the API Server
```bash
wa-meta-ai serve
```
- Interactive API docs: `http://127.0.0.1:8000/docs`
- First run: scan the WhatsApp Web QR code in the opened browser window (or at `http://127.0.0.1:8000/auth/qr`). Your session persists in `wa_profile/`.

---

## Usage Examples

### REST API (`POST /generate`)
```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Generate 5 minimalist claymation space cats",
    "series": "space_cats",
    "format": "png",
    "target_count": 5
  }'
```

### CLI
```bash
wa-meta-ai generate "Generate 4 cyberpunk street scenes" --series cyberpunk --format png
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

### Docker
```bash
docker compose up --build -d
# Open http://127.0.0.1:8000/auth/qr to scan the QR code
```

### MCP Server (for Antigravity, Claude Desktop, Cursor, etc.)
Add to your `mcp_config.json`:
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
Exposes `auth_status`, `generate_images`, `generate_batch`, `extract_history`, `clear_chat`, and `list_media` to any MCP-compatible AI agent.

---

## Configuration (`.env`)

See [`.env.example`](.env.example) for all environment variables (`WA_PORT`, `WA_DOWNLOADS_DIR` defaulting to `~/Downloads/wa-meta-ai`, `WA_SAVE_MANIFEST=false`, `WA_DEFAULT_FORMAT`, `WA_COOLDOWN_SEC`, `WA_JITTER_SEC`, `WA_META_AI_API_KEY`, `WA_CDP_URL`, `WA_HEADLESS`). `.env` in the working directory is loaded automatically.
