"""Configuration settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _default_profile_dir() -> Path:
    if env := os.getenv("WA_PROFILE_DIR"):
        return Path(env).expanduser()
    local = Path("wa_profile")
    if local.exists():
        return local.resolve()
    return Path.home() / ".wa-meta-ai" / "profile"


@dataclass
class Settings:
    """Runtime configuration for WhatsApp Meta AI API."""

    profile_dir: Path = field(default_factory=_default_profile_dir)
    downloads_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("WA_DOWNLOADS_DIR", str(Path.home() / "Downloads" / "wa-meta-ai"))
        )
    )
    save_manifest: bool = field(
        default_factory=lambda: os.getenv("WA_SAVE_MANIFEST", "false").lower() in ("1", "true", "yes")
    )
    cdp_url: str = field(default_factory=lambda: os.getenv("WA_CDP_URL", ""))
    default_format: str = field(default_factory=lambda: os.getenv("WA_DEFAULT_FORMAT", "png"))
    cooldown_sec: float = field(default_factory=lambda: float(os.getenv("WA_COOLDOWN_SEC", "2.0")))
    jitter_sec: float = field(default_factory=lambda: float(os.getenv("WA_JITTER_SEC", "1.5")))
    api_key: str = field(default_factory=lambda: os.getenv("WA_META_AI_API_KEY", ""))
    host: str = field(default_factory=lambda: os.getenv("WA_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("WA_PORT", "8000")))
    headless: bool = field(
        default_factory=lambda: os.getenv("WA_HEADLESS", "false").lower() in ("1", "true", "yes")
    )
