"""Pair Slips pools with the equivalent game and line on Polymarket."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from .polymarket import PolyGame, PolyMarket, PolyOutcome, Team
from .slips import TEAM_LINE, SlipsMarket, SlipsSide

# How far apart the two platforms' listed start times may be and still be the same game.
MAX_START_GAP = timedelta(hours=12)
# Wider window when either side lists exactly midnight UTC, which is often a date-only placeholder.
MAX_START_GAP_PLACEHOLDER = timedelta(hours=36)

# Map loose sport labels Slips might use onto Polymarket league slugs.
SPORT_HINTS = {
    "nfl": ["nfl"], "football": ["nfl", "cfb"], "ncaaf": ["cfb"], "college football": ["cfb"],
    "nba": ["nba"], "basketball": ["nba", "wnba", "cbb"], "wnba": ["wnba"], "ncaab": ["cbb"],
    "mlb": ["mlb"], "baseball": ["mlb"], "nhl": ["nhl"], "hockey": ["nhl"],
    "soccer": ["mls", "epl"], "mls": ["mls"], "epl": ["epl"], "premier league": ["epl"],
    "ufc": ["ufc"], "mma": ["ufc"],
}

STOPWORDS = {"the", "fc", "sc", "cf", "afc", "vs", "v", "at", "win", "wins", "to", "will", "moneyline", "ml"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return " ".join(w for w in text.split() if w not in STOPWORDS)


@dataclass
class Match:
    slips: SlipsMarket
    poly: PolyGame
    market: PolyMarket  # the specific Polymarket market this pool lines up with
    # pairs of (slips side, polymarket outcome) that refer to the same result
    pairs: list[tuple[SlipsSide, PolyOutcome]]


class TeamIndex:
    """Resolves Polymarket outcome labels ("Bills") to full team records per league."""

    def __init__(self, teams_by_league: dict[str, list[Team]]):
        self.teams_by_league = teams_by_league

    def resolve(self, league: str, label: str) -> Team | None:
        n = norm(label)
        for t in self.teams_by_league.get(league, []):
            if n in {norm(t.name), norm(t.alias), t.abbreviation}:
                return t
        for t in self.teams_by_league.get(league, []):
            alias = norm(t.alias)
            if alias and re.search(rf"\b{re.escape(alias)}\b", n):
                return t
        return None


def _names_for(outcome: PolyOutcome) -> set[str]:
    names = {norm(outcome.name)}
    if outcome.team:
        names |= {norm(outcome.team.name), norm(outcome.team.alias)}
        # "Buffalo Bills" -> also accept "Buffalo"
        city = norm(outcome.team.name.replace(outcome.team.alias, "")) if outcome.team.alias else ""
        if city:
            names.add(city)
    return {n for n in names if n}


def side_score(side: SlipsSide, outcome: PolyOutcome) -> int:
    """How confidently a Slips side name refers to a Polymarket outcome. 0 = no match."""
    s = norm(side.name)
    if not s:
        return 0
    best = 0
    for name in _names_for(outcome):
        if s == name:
            best = max(best, 3)
        elif re.search(rf"\b{re.escape(name)}\b", s) or re.search(rf"\b{re.escape(s)}\b", name):
            best = max(best, 2 if len(name) >= 4 and len(s) >= 4 else 1)
    if outcome.team and s == outcome.team.abbreviation:
        best = max(best, 2)
    return best


def find_game(slips: SlipsMarket, games: list[PolyGame]) -> tuple[PolyGame, dict[str, PolyOutcome]] | None:
    """Find the Polymarket game for a Slips pool. Returns the game and a map of Slips team name -> Polymarket team outcome."""
    teams = [t for t in slips.teams if norm(t) not in ("draw", "tie")]
    if len(teams) != 2:
        return None
    leagues = None
    for key, value in SPORT_HINTS.items():
        if key and key in slips.sport.lower():
            leagues = set(value)
            break

    probes = [SlipsSide(name=t, percentage=0) for t in teams]
    best: tuple[tuple[int, float], PolyGame, dict[str, PolyOutcome]] | None = None
    for game in games:
        if leagues and game.league not in leagues:
            continue
        gap = abs(slips.start - game.start) if slips.start and game.start else None
        if gap is not None:
            placeholder = any(t.hour == 0 and t.minute == 0 for t in (slips.start, game.start))
            if gap > (MAX_START_GAP_PLACEHOLDER if placeholder else MAX_START_GAP):
                continue
        a, b = game.outcomes
        for (x, y) in ((a, b), (b, a)):
            s0, s1 = side_score(probes[0], x), side_score(probes[1], y)
            # Both teams must match; require one strong match so "Giants" alone can't cross leagues.
            if s0 == 0 or s1 == 0 or max(s0, s1) < 2:
                continue
            # Rank by name confidence, then by closest start time (teams play series on back-to-back days).
            score = (s0 + s1, -gap.total_seconds() if gap is not None else float("-inf"))
            if best is None or score > best[0]:
                best = (score, game, {teams[0]: x, teams[1]: y})
    return (best[1], best[2]) if best else None


def _team_of(side_name: str, team_map: dict[str, PolyOutcome]) -> PolyOutcome | None:
    """Which Polymarket team a Slips side like "Bills -4.5" or "Buffalo Bills" refers to."""
    probe = SlipsSide(name=TEAM_LINE.sub("", side_name), percentage=0)
    scored = [(side_score(probe, o), o) for o in team_map.values()]
    scored.sort(key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def _same_team(a: str, outcome: PolyOutcome) -> bool:
    return side_score(SlipsSide(name=a, percentage=0), outcome) >= 2 or norm(a) == norm(outcome.name)


def match_market(slips: SlipsMarket, games: list[PolyGame]) -> Match | None:
    found = find_game(slips, games)
    if not found:
        return None
    game, team_map = found
    kind = slips.kind

    if kind == "moneyline":
        pairs = []
        for side in slips.sides:
            team = _team_of(side.name, team_map)
            if team is None:
                if norm(side.name) in ("draw", "tie"):
                    continue
                return None
            pairs.append((side, next(o for o in game.outcomes if o is team)))
        if len(pairs) != 2 or pairs[0][1] is pairs[1][1]:
            return None
        market = next(m for m in game.markets if m.kind == "moneyline")
        return Match(slips, game, market, pairs)

    if kind == "spread":
        for m in (m for m in game.markets if m.kind == "spreads" and m.line is not None):
            pairs = []
            for side in slips.sides:
                team = _team_of(side.name, team_map)
                if team is None or side.spread is None:
                    break
                # Polymarket's line belongs to outcomes[0]; outcomes[1] carries the opposite line.
                for idx, outcome in enumerate(m.outcomes):
                    line = m.line if idx == 0 else -m.line
                    if _same_team(team.name, outcome) and abs(line - side.spread) < 1e-9:
                        pairs.append((side, outcome))
            if len(pairs) == 2 and pairs[0][1] is not pairs[1][1]:
                return Match(slips, game, m, pairs)
        return None

    if kind == "total":
        for m in (m for m in game.markets if m.kind == "totals" and m.line is not None):
            if slips.line is None or abs(m.line - slips.line) > 1e-9:
                continue
            pairs = [(side, o) for side in slips.sides for o in m.outcomes if norm(side.name) == norm(o.name)]
            if len(pairs) == 2:
                return Match(slips, game, m, pairs)
        return None

    if kind == "prop":
        # Polymarket rarely lists player props; match only when the question names the same player/stat and line.
        subject = norm(re.sub(r"\s+is\s+[\d.]+$", "", slips.sub_title))
        words = [w for w in subject.split() if len(w) > 2]
        if not words or slips.line is None:
            return None
        for m in game.markets:
            if m.line is None or abs(m.line - slips.line) > 1e-9:
                continue
            question = norm(m.question)
            if all(re.search(rf"\b{re.escape(w)}", question) for w in words):
                pairs = [(side, o) for side in slips.sides for o in m.outcomes if norm(side.name) == norm(o.name)]
                if len(pairs) == 2:
                    return Match(slips, game, m, pairs)
        return None
    return None
