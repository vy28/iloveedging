"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("ILOVEEDGING_HOME", Path.cwd())).expanduser().resolve()
STATE_DIR = ROOT / ".state"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on") if value else default


@dataclass
class Settings:
    # Slips auth (copied from a logged-in browser session, see README)
    slips_access_token: str = ""
    slips_refresh_token: str = ""

    # Alert when Polymarket probability exceeds Slips probability by this many points (0.10 = 10pp)
    edge_threshold: float = 0.08
    # Also require at least this expected profit per $1 spent on Slips, after fees and your own bet (0.05 = +5%)
    min_ev: float = 0.05
    # The bet you'd place, in dollars. Slips pools are small, so your own bet moves the odds.
    bet_size: float = 10.0
    # Skip pools with less than this much money on the other side (that's the most you can win)
    min_opposing_money: float = 25.0
    # Ignore Polymarket markets thinner than this (USD traded)
    min_poly_volume: float = 1000.0
    # Ignore Polymarket markets whose bid/ask spread is wider than this
    max_poly_spread: float = 0.05
    # How far ahead to look for games, in days
    lookahead_days: float = 10.0
    leagues: list[str] = field(default_factory=lambda: ["nfl", "nba", "mlb", "nhl", "wnba", "cfb", "cbb", "mls", "epl", "ufc"])

    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    macos_notifications: bool = False

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        s = cls()
        s.slips_access_token = os.environ.get("SLIPS_ACCESS_TOKEN", "").strip()
        s.slips_refresh_token = os.environ.get("SLIPS_REFRESH_TOKEN", "").strip()
        s.edge_threshold = _float("EDGE_THRESHOLD", s.edge_threshold)
        s.min_ev = _float("MIN_EV", s.min_ev)
        s.bet_size = _float("BET_SIZE", s.bet_size)
        s.min_opposing_money = _float("MIN_OPPOSING_MONEY", s.min_opposing_money)
        s.min_poly_volume = _float("MIN_POLY_VOLUME", s.min_poly_volume)
        s.max_poly_spread = _float("MAX_POLY_SPREAD", s.max_poly_spread)
        s.lookahead_days = _float("LOOKAHEAD_DAYS", s.lookahead_days)
        if os.environ.get("LEAGUES"):
            s.leagues = [x.strip().lower() for x in os.environ["LEAGUES"].split(",") if x.strip()]
        s.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        s.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        s.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        s.macos_notifications = _bool("MACOS_NOTIFICATIONS", s.macos_notifications)
        return s
