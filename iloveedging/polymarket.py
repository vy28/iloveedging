"""Read-only client for Polymarket's public Gamma API (no account needed)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
PAGE = 100

# Full-game market types we can line up against Slips pools. Quarter/half markets are ignored
# because Slips' spread and total pools are for the whole game.
GAME_MARKET_TYPES = {"moneyline", "spreads", "totals"}


@dataclass
class Team:
    league: str
    name: str  # "Buffalo Bills"
    alias: str  # "Bills"
    abbreviation: str  # "buf"


@dataclass
class PolyOutcome:
    name: str  # outcome label as Polymarket shows it, e.g. "Bills" or "Over"
    prob: float  # mid price, 0..1
    bid: float | None
    ask: float | None
    team: Team | None = None


@dataclass
class PolyMarket:
    kind: str  # "moneyline" | "spreads" | "totals" | any other sportsMarketType (props)
    question: str
    line: float | None  # spreads: applies to outcomes[0] (outcomes[1] gets -line); totals: the O/U number
    volume: float
    spread: float | None  # bid/ask width
    outcomes: list[PolyOutcome] = field(default_factory=list)


@dataclass
class PolyGame:
    league: str
    title: str
    slug: str
    start: datetime | None
    volume: float  # moneyline volume
    spread: float | None  # moneyline bid/ask width
    outcomes: list[PolyOutcome] = field(default_factory=list)  # moneyline outcomes: the two teams
    markets: list[PolyMarket] = field(default_factory=list)  # every market on the event, moneyline included

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.slug}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace(" ", "T")
    if value.endswith("+00"):
        value += ":00"
    value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market(m: dict) -> PolyMarket | None:
    try:
        names = json.loads(m.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    except (ValueError, TypeError):
        return None
    if len(names) != 2 or len(prices) != 2 or m.get("closed"):
        return None
    # bestBid/bestAsk are quoted for the first outcome; the second is the mirror image.
    bid, ask = _num(m.get("bestBid")), _num(m.get("bestAsk"))
    width = (ask - bid) if bid is not None and ask is not None else None
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        p0 = (bid + ask) / 2
        quotes = [(p0, bid, ask), (1 - p0, 1 - ask, 1 - bid)]
    else:
        quotes = [(prices[0], None, None), (prices[1], None, None)]
    return PolyMarket(
        kind=m.get("sportsMarketType") or "other",
        question=m.get("question") or "",
        line=_num(m.get("line")),
        volume=_num(m.get("volumeNum")) or 0.0,
        spread=width,
        outcomes=[PolyOutcome(n, p, b, a) for n, (p, b, a) in zip(names, quotes)],
    )


class PolymarketClient:
    def __init__(self, session: requests.Session | None = None):
        self.http = session or requests.Session()
        self.http.headers.setdefault("User-Agent", "iloveedging/0.1.0")

    def _get(self, path: str, **params):
        r = self.http.get(f"{GAMMA}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def teams(self, league: str) -> list[Team]:
        out: list[Team] = []
        offset = 0
        while True:
            batch = self._get("/teams", league=league, limit=PAGE, offset=offset)
            for t in batch:
                out.append(Team(league, t.get("name") or "", t.get("alias") or "", (t.get("abbreviation") or "").lower()))
            if len(batch) < PAGE:
                return out
            offset += PAGE

    def games(self, league: str, lookahead_days: float) -> list[PolyGame]:
        """Upcoming head-to-head games in a league that have a moneyline market."""
        now = datetime.now(timezone.utc)
        params = {
            "tag_slug": league,
            "closed": "false",
            "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date_max": (now + timedelta(days=lookahead_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": PAGE,
        }
        games: list[PolyGame] = []
        offset = 0
        while True:
            events = self._get("/events", offset=offset, **params)
            for event in events:
                game = self._game_from_event(league, event)
                if game:
                    games.append(game)
            if len(events) < PAGE:
                return games
            offset += PAGE

    @staticmethod
    def _game_from_event(league: str, event: dict) -> PolyGame | None:
        title = event.get("title") or ""
        raw = [m for m in event.get("markets", []) if not m.get("closed")]
        moneyline_raw = next((m for m in raw if m.get("sportsMarketType") == "moneyline"), None)
        if not moneyline_raw or "season series" in title.lower():
            return None
        moneyline = _market(moneyline_raw)
        if not moneyline:
            return None
        markets = [pm for pm in (_market(m) for m in raw) if pm]
        return PolyGame(
            league=league,
            title=title,
            slug=event.get("slug") or "",
            start=_parse_dt(moneyline_raw.get("gameStartTime") or event.get("startTime") or event.get("endDate")),
            volume=moneyline.volume or _num(event.get("volume")) or 0.0,
            spread=moneyline.spread,
            outcomes=moneyline.outcomes,
            markets=markets,
        )
