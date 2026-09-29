"""Read-only client for the Slips web app API (api.startslips.com).

The sports feeds require a logged-in session. We reuse the access/refresh
token pair the slips.com web app stores in the browser, and refresh the access
token the same way the web app does when it expires.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import STATE_DIR

API = "https://api.startslips.com/app"
TOKEN_FILE = STATE_DIR / "slips_tokens.json"

# Feeds the web app calls to render sports pools. We read all of them and
# de-duplicate by drawing id, because each surfaces a different slice.
FEEDS = [
    "feed/sports/with-tourlays",
    "feed/sports/open-pools",
    "feed/sports/live/with-futures",
    "feed/discovery",
    "feed/for-you",
    "spotlights/with-bet-pools",
    "home",
]

TEAM_LINE = re.compile(r"\s*[+-]\d+(\.\d+)?\s*$")  # "Bills -4.5" -> "Bills"

NON_MONEYLINE = re.compile(r"spread|total|over|under|o/u|points|yards|prop|handicap|first|half|quarter|period|inning", re.I)


class SlipsAuthError(RuntimeError):
    pass


@dataclass
class SlipsSide:
    name: str
    percentage: float  # "chance" shown in the app, 0..100
    slippers: int | None = None
    bet_amount: float | None = None
    odds_to_win: float | None = None
    spread: float | None = None
    id: int | None = None


@dataclass
class SlipsMarket:
    id: int
    title: str
    subtitle: str
    sport: str
    start: datetime | None
    is_live: bool
    market_label: str
    sides: list[SlipsSide] = field(default_factory=list)
    share_link: str = ""
    market_type: str = ""  # Slips' own label: "Win" for moneylines, "Spread", "Total Points", ...
    fee_percent: float | None = None  # fee added on top of each bet, e.g. 0.05
    fee_min: float | None = None  # minimum fee per bet in dollars
    my_side_id: int | None = None  # set if you already have a bet in this pool
    line: float | None = None  # totals/props: the over/under number
    prop_category: str = ""  # "game" or "player"
    sub_title: str = ""  # e.g. "Jahmyr Gibbs touchdowns is 2.5"
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        if self.share_link.startswith("/"):
            return "https://slips.com" + self.share_link
        return self.share_link

    @property
    def pool_total(self) -> float | None:
        amounts = [s.bet_amount for s in self.sides]
        return None if any(a is None for a in amounts) else sum(amounts)

    @property
    def kind(self) -> str:
        """"moneyline", "spread", "total", or "prop"."""
        label = self.market_type.strip().lower()
        if any(s.spread for s in self.sides) or "spread" in label:
            return "spread"
        over_under = {s.name.strip().lower() for s in self.sides} <= {"over", "under"}
        if self.prop_category == "player" or (over_under and label and "total" not in label):
            return "prop"
        if over_under or "total" in label:
            return "total"
        if label and label not in ("win", "moneyline", "winner", "match winner"):
            return "prop"
        if not label and NON_MONEYLINE.search(self.market_label):
            return "prop"
        return "moneyline"

    @property
    def is_moneyline(self) -> bool:
        return self.kind == "moneyline"

    @property
    def teams(self) -> list[str]:
        """The two teams in the game, from the title ("Lions vs Bills") or the side names."""
        # Side names are the most specific ("Houston Cougars"); titles often use nicknames ("Cougars vs Red Raiders").
        if self.kind in ("moneyline", "spread"):
            names = [TEAM_LINE.sub("", s.name).strip() for s in self.sides if s.name.strip().lower() not in ("draw", "tie")]
            if len(names) == 2:
                return names
        parts = re.split(r"\s+(?:vs\.?|v\.?|@|at)\s+", self.title, flags=re.I)
        if len(parts) == 2 and all(parts):
            return [p.strip() for p in parts]
        return [TEAM_LINE.sub("", s.name).strip() for s in self.sides]

    @property
    def description(self) -> str:
        """Human label for the bet, e.g. "Moneyline", "Spread", "Total 53.5", "Jahmyr Gibbs touchdowns 2.5"."""
        if self.kind == "moneyline":
            return "Moneyline"
        if self.kind == "spread":
            return "Spread"
        if self.kind == "total":
            return f"Total {self.line:g}" if self.line is not None else "Total"
        subject = re.sub(r"\s+is\s+[\d.]+$", "", self.sub_title).strip() or self.market_type
        return f"{subject} {self.line:g}" if self.line is not None else subject


def _g(d: dict, *keys, default=None):
    """Read a field that may be snake_case (raw API) or camelCase."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    # Slips format: "2026-09-18 00:15:00 +0000"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(value.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
    return None


def parse_markets(payload) -> list[SlipsMarket]:
    """Walk any Slips JSON response and pull out every two/three-way sports pool."""
    found: dict[int, SlipsMarket] = {}

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        market = _market_from_drawing(node)
        if market and market.id not in found:
            found[market.id] = market
        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value)

    visit(payload)
    return list(found.values())


def _market_from_drawing(d: dict) -> SlipsMarket | None:
    options = _g(d, "number_types", "numberTypes")
    if not isinstance(options, list) or len(options) < 2 or d.get("id") is None:
        return None
    if _g(d, "is_complete", "isComplete") or _g(d, "is_canceled", "isCanceled"):
        return None

    sides = []
    for o in options:
        if not isinstance(o, dict):
            continue
        pct = _num(_g(o, "bet_percentage", "betPercentage", "percentage"))
        name = _g(o, "title", "name", default="")
        if pct is None or not name:
            continue
        sides.append(
            SlipsSide(
                name=str(name),
                percentage=pct,
                slippers=int(_num(_g(o, "total_pool_slippers", "totalPoolSlippers", "slipper_count", "slipperCount")) or 0) or None,
                bet_amount=_num(_g(o, "bet_amount", "betAmount")),
                odds_to_win=_num(_g(o, "odds_to_win", "oddsToWin")),
                spread=_num(_g(o, "spread")) or None,
                id=int(o["id"]) if isinstance(o.get("id"), int) else None,
            )
        )
    if len(sides) < 2:
        return None

    label = " ".join(
        str(x)
        for x in (
            _g(d, "bet_market_name", "betMarketName"),
            _g(d, "bet_market_type", "betMarketType"),
            _g(d, "market_type", "marketType"),
            _g(d, "sub_bet_title", "subBetTitle"),
            _g(d, "option_title", "optionTitle"),
        )
        if x and not isinstance(x, (dict, list))
    )
    sport = _g(d, "sport", default="")
    if isinstance(sport, dict):
        sport = _g(sport, "name", "slug", "title", default="")
    game = _g(d, "game", default={}) or {}
    if not isinstance(game, dict):
        game = {}
    if not sport:
        sport = _g(game, "name", "title", default="")
    market_type = _g(d, "market_type", "marketType", default={})
    market_type = _g(market_type, "label", default="") if isinstance(market_type, dict) else ""
    my_bet = _g(d, "is_user_bet", "isUserBet")

    return SlipsMarket(
        id=int(d["id"]),
        title=str(_g(d, "title", "name", default="")),
        subtitle=str(_g(d, "subtitle", default="") or ""),
        sport=str(sport or ""),
        start=_parse_dt(_g(d, "drawing_at", "drawingAt", "drawing_closes_at", "drawingClosesAt")),
        is_live=bool(_g(d, "is_live", "isLive", default=False)),
        market_label=label,
        sides=sides,
        share_link=str(_g(d, "share_link", "shareLink", default="") or ""),
        market_type=str(market_type or ""),
        fee_percent=_num(_g(game, "fee_percent", "feePercent")),
        fee_min=_num(_g(game, "fee_min", "feeMin")),
        my_side_id=_g(d, "selected_number_type_id", "selectedNumberTypeId") if my_bet else None,
        line=_num(_g(d, "bet_market_value", "betMarketValue")) if _g(d, "bet_market_value", "betMarketValue") not in (None, "") else (_num(_g(d, "comparison_basis", "comparisonBasis")) or None),
        prop_category=str(_g(d, "prop_category_type", "propCategoryType", default="") or "").lower(),
        sub_title=str(_g(d, "sub_bet_title", "subBetTitle", default="") or ""),
        raw=d,
    )


class SlipsClient:
    def __init__(self, access_token: str, refresh_token: str, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.headers.update({"Accept": "application/json", "User-Agent": "Mozilla/5.0 iloveedging/0.1.0"})
        saved = self._load_saved_tokens()
        self.env_refresh_token = refresh_token
        # Reuse tokens refreshed on a previous run, unless you've since pasted new ones into .env.
        if saved.get("env_refresh") == refresh_token and saved.get("access"):
            self.access_token, self.refresh_token = saved["access"], saved.get("refresh") or refresh_token
        else:
            self.access_token, self.refresh_token = access_token, refresh_token
        if not self.access_token and not self.refresh_token:
            raise SlipsAuthError("No Slips token configured. Set SLIPS_ACCESS_TOKEN / SLIPS_REFRESH_TOKEN in .env (see README).")

    @staticmethod
    def _load_saved_tokens() -> dict:
        try:
            return json.loads(TOKEN_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _save_tokens(self) -> None:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({"access": self.access_token, "refresh": self.refresh_token, "env_refresh": self.env_refresh_token}))
        TOKEN_FILE.chmod(0o600)

    def refresh(self) -> None:
        if not self.refresh_token:
            raise SlipsAuthError("Slips access token expired and no refresh token is set.")
        # The web app sends {"token": ...}; the server also accepts an OAuth-style body, so try both.
        for body_in in ({"token": self.refresh_token}, {"grant_type": "refresh_token", "refresh_token": self.refresh_token}):
            r = self.http.post(f"{API}/auth/refresh-token", json=body_in, timeout=20)
            try:
                body = r.json()
            except ValueError:
                body = {}
            data = body.get("data", body) if isinstance(body, dict) else {}
            access = _g(data, "accessToken", "access_token") or {} if isinstance(data, dict) else {}
            refresh = _g(data, "refreshToken", "refresh_token") or {} if isinstance(data, dict) else {}
            if r.status_code == 200 and access:
                break
        if r.status_code != 200 or not access:
            reason = str(body.get("message") if isinstance(body, dict) else r.text[:200]).rstrip(".")
            raise SlipsAuthError(f"Slips token refresh failed ({r.status_code}): {reason}. Log in on slips.com and copy fresh tokens.")
        self.access_token = access.get("token") if isinstance(access, dict) else access
        if refresh:
            self.refresh_token = refresh.get("token") if isinstance(refresh, dict) else refresh
        self._save_tokens()

    def get(self, path: str, **params):
        for attempt in range(2):
            headers = {"token": self.access_token} if self.access_token else {}
            r = self.http.get(f"{API}/{path}", params=params, headers=headers, timeout=20)
            if r.status_code == 401 and attempt == 0:
                self.refresh()
                continue
            if r.status_code == 401:
                raise SlipsAuthError("Slips rejected the token even after refreshing. Copy fresh tokens from slips.com.")
            r.raise_for_status()
            return r.json()

    def fetch_feeds(self, dump_dir: Path | None = None) -> tuple[list[SlipsMarket], dict[str, str]]:
        """Pull every sports feed. Returns markets plus a per-feed status line."""
        markets: dict[int, SlipsMarket] = {}
        status: dict[str, str] = {}
        for feed in FEEDS:
            try:
                payload = self.get(feed)
            except SlipsAuthError:
                raise
            except requests.RequestException as e:
                status[feed] = f"error: {e}"
                continue
            if dump_dir:
                dump_dir.mkdir(parents=True, exist_ok=True)
                (dump_dir / (feed.replace("/", "_") + ".json")).write_text(json.dumps(payload, indent=2))
            parsed = parse_markets(payload)
            status[feed] = f"{len(parsed)} pools"
            for m in parsed:
                markets.setdefault(m.id, m)
        return list(markets.values()), status
