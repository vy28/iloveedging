"""Compare matched markets and rank their modeled pricing edges."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .matching import Match, match_market
from .polymarket import PolyGame, PolyMarket
from .slips import SlipsMarket, SlipsSide

# Slips' standard fee when a pool doesn't say: 5% of the stake, at least $0.50.
DEFAULT_FEE_PERCENT = 0.05
DEFAULT_FEE_MIN = 0.50


@dataclass
class Opportunity:
    slips: SlipsMarket
    poly: PolyGame
    market: PolyMarket  # the Polymarket market used for the price
    side: str  # what to back on Slips: "Buffalo Bills", "Bills -4.5", "Over"
    poly_side: str
    slips_prob: float  # share of the pool on this side right now, 0..1
    poly_prob: float  # 0..1
    edge: float  # poly_prob - slips_prob
    stake: float  # bet size the payout was computed for
    fee: float  # fee on top of the stake
    win_amount: float  # total returned if this side wins (stake + share of the other side)
    payout: float  # win_amount / (stake + fee): return per $1 actually spent
    ev: float  # expected profit per $1 spent
    opposing_money: float | None  # money on the other side(s) — the most you can ever win
    already_bet: bool = False
    skip_reason: str = ""  # why it didn't qualify, empty if it did

    @property
    def key(self) -> str:
        return f"{self.slips.id}:{self.side}"

    @property
    def kind(self) -> str:
        return self.slips.kind

    @property
    def pick(self) -> str:
        """Short label for the bet, e.g. "Buffalo Bills", "Bills -4.5", "Over 53.5", "Jahmyr Gibbs touchdowns Over 2.5"."""
        if self.kind == "total":
            return f"{self.side} {self.slips.line:g}" if self.slips.line is not None else self.side
        if self.kind == "prop":
            subject = self.slips.description.rsplit(" ", 1)[0] if self.slips.line is not None else self.slips.description
            return f"{subject} {self.side}" + (f" {self.slips.line:g}" if self.slips.line is not None else "")
        return self.side


def slips_fee(market: SlipsMarket, stake: float) -> float:
    pct = market.fee_percent if market.fee_percent is not None else DEFAULT_FEE_PERCENT
    minimum = market.fee_min if market.fee_min is not None else DEFAULT_FEE_MIN
    return max(minimum, stake * pct)


def win_amount(side: SlipsSide, pool_total: float | None, stake: float) -> float:
    """What you get back if you add `stake` to `side` and it wins.

    Slips pools are pari-mutuel: winners split the whole pool pro rata. Adding
    your own money shrinks your odds, which matters a lot in $100 pools.
    """
    if pool_total is not None and side.bet_amount is not None:
        return stake * (pool_total + stake) / (side.bet_amount + stake)
    # No dollar amounts in the response: fall back to the odds Slips displays.
    odds = side.odds_to_win if side.odds_to_win and side.odds_to_win > 1 else 100 / side.percentage if side.percentage else 1
    return stake * odds


def evaluate(match: Match, settings: Settings) -> list[Opportunity]:
    market = match.slips
    pool = market.pool_total
    stake = settings.bet_size
    total_pct = sum(s.percentage for s, _ in match.pairs)
    opps = []
    for side, outcome in match.pairs:
        slips_prob = side.percentage / total_pct if total_pct else 0.0
        opposing = (pool - side.bet_amount) if pool is not None and side.bet_amount is not None else None
        fee = slips_fee(market, stake)
        back = win_amount(side, pool, stake)
        spent = stake + fee
        opps.append(
            Opportunity(
                slips=market,
                poly=match.poly,
                market=match.market,
                side=side.name,
                poly_side=outcome.name,
                slips_prob=slips_prob,
                poly_prob=outcome.prob,
                edge=outcome.prob - slips_prob,
                stake=stake,
                fee=fee,
                win_amount=back,
                payout=back / spent,
                ev=(outcome.prob * back - spent) / spent,
                opposing_money=opposing,
                already_bet=market.my_side_id is not None and market.my_side_id == side.id,
            )
        )
    return opps


def _skip_reason(o: Opportunity, settings: Settings) -> str:
    if o.slips.is_live:
        return "game is live"
    if o.opposing_money is not None and o.opposing_money < settings.min_opposing_money:
        return f"only ${o.opposing_money:g} on the other side"
    if o.market.volume < settings.min_poly_volume:
        return f"thin Polymarket (${o.market.volume:,.0f} vol)"
    if o.market.spread is not None and o.market.spread > settings.max_poly_spread:
        return f"wide Polymarket bid/ask ({o.market.spread:.2f})"
    if o.edge < settings.edge_threshold:
        return ""  # just not mispriced enough; no need to explain
    if o.ev < settings.min_ev:
        return f"EV {o.ev:+.0%} after your ${o.stake:g} bet and fee"
    return ""


@dataclass
class ScanResult:
    opportunities: list[Opportunity]  # passed thresholds, best first
    all_compared: list[Opportunity]  # every matched side, for the table view
    unmatched: list[SlipsMarket]
    skipped_empty: int


def scan(slips_markets: list[SlipsMarket], games: list[PolyGame], settings: Settings) -> ScanResult:
    compared: list[Opportunity] = []
    unmatched: list[SlipsMarket] = []
    empty = 0
    for market in slips_markets:
        if market.pool_total == 0 or all(s.percentage == 0 for s in market.sides):
            empty += 1
            continue
        match = match_market(market, games)
        if not match:
            unmatched.append(market)
            continue
        compared.extend(evaluate(match, settings))

    hits = []
    for o in compared:
        o.skip_reason = _skip_reason(o, settings)
        if not o.skip_reason and o.edge >= settings.edge_threshold and o.ev >= settings.min_ev:
            hits.append(o)
    hits.sort(key=lambda o: o.ev, reverse=True)
    compared.sort(key=lambda o: o.edge, reverse=True)
    return ScanResult(hits, compared, unmatched, empty)
