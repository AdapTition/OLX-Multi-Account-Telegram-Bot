import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"Переменная {name} должна быть числом, сейчас: {raw!r}")


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int | None
    db_path: Path
    poll_interval: int
    olx_domain: str
    olx_client_id: str
    olx_client_secret: str
    olx_redirect_uri: str


def load_config() -> Config:
    load_dotenv(ENV_PATH)
    owner = os.getenv("OWNER_ID", "").strip()
    db_path = Path(os.getenv("DB_PATH", "").strip() or BASE_DIR / "data" / "olx_bot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        owner_id=int(owner) if owner.isdigit() else None,
        db_path=db_path,
        # OLX не присылает вебхуков о новых сообщениях, поэтому опрашиваем.
        # 10 секунд — практически мгновенно и не нагружает API.
        poll_interval=max(5, _int("POLL_INTERVAL", 10)),
        olx_domain=os.getenv("OLX_DOMAIN", "").strip() or "www.olx.ua",
        olx_client_id=os.getenv("OLX_CLIENT_ID", "").strip(),
        olx_client_secret=os.getenv("OLX_CLIENT_SECRET", "").strip(),
        olx_redirect_uri=os.getenv("OLX_REDIRECT_URI", "").strip() or "https://example.com/",
    )
