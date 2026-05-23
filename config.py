from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


@dataclass(slots=True, frozen=True)
class Settings:
    bot_token: str
    db_path: Path
    gemini_api_key: str
    gemini_text_model: str
    gemini_image_model: str
    max_history_messages: int
    temperature: float
    max_tokens: int
    webhook_base_url: str
    webhook_path: str
    webhook_secret: str
    http_host: str
    http_port: int
    run_mode: str


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parent
    if load_dotenv is not None:
        load_dotenv(base_dir / ".env")

    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    render_external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    space_host = os.getenv("SPACE_HOST", "").strip()
    webhook_base_url = os.getenv("WEBHOOK_BASE_URL", "").strip()
    if not webhook_base_url and render_external_url:
        webhook_base_url = render_external_url
    if not webhook_base_url and space_host:
        webhook_base_url = f"https://{space_host}"

    webhook_path = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"
    run_mode = os.getenv("BOT_RUN_MODE", "").strip().casefold()
    if not run_mode:
        run_mode = "webhook" if webhook_base_url else "polling"

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        db_path=Path(os.getenv("DB_PATH", str(base_dir / "rpg_bot.sqlite3"))),
        gemini_api_key=gemini_api_key,
        gemini_text_model=os.getenv("GEMINI_TEXT_MODEL", "gemini-2.5-flash-lite").strip(),
        gemini_image_model=os.getenv(
            "GEMINI_IMAGE_MODEL",
            "gemini-2.0-flash-preview-image-generation",
        ).strip(),
        max_history_messages=int(os.getenv("MAX_HISTORY_MESSAGES", "8")),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.9")),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "320")),
        webhook_base_url=webhook_base_url,
        webhook_path=webhook_path if webhook_path.startswith("/") else f"/{webhook_path}",
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        http_host=os.getenv("HTTP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        http_port=int(os.getenv("PORT", os.getenv("HTTP_PORT", "7860"))),
        run_mode=run_mode,
    )


settings = load_settings()
