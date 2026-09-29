"""Offline tests: python -m unittest discover tests"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from iloveedging.config import Settings
from iloveedging.matching import match_market
from iloveedging.polymarket import PolyGame, PolyMarket, PolymarketClient, PolyOutcome, Team
from iloveedging.scanner import scan
from iloveedging.slips import parse_markets

ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


def game(league, title, a, pa, b, start, ta=None, tb=None, volume=10_000, extra=()):
    outcomes = [PolyOutcome(a, pa, None, None, ta), PolyOutcome(b, 1 - pa, None, None, tb)]
    markets = [PolyMarket("moneyline", title, None, volume, 0.01, outcomes), *extra]
    return PolyGame(league, title, title.lower().replace(" ", "-"), start, volume, 0.01, outcomes, markets)


LIONS = Team("nfl", "Detroit Lions", "Lions", "det")
BILLS = Team("nfl", "Buffalo Bills", "Bills", "buf")
THU = datetime(2026, 9, 18, 0, 15, tzinfo=UTC)


class ParseTests(unittest.TestCase):
    def test_sample_feed(self):
        markets = parse_markets(json.loads((ROOT / "iloveedging/data/slips_sample.json").read_text()))
        by_id = {m.id: m for m in markets}
        self.assertEqual(len(markets), 6)
        lions_bills = by_id[900001]
        self.assertEqual([s.percentage for s in lions_bills.sides], [42, 58])
        self.assertEqual(lions_bills.start, THU)
        self.assertEqual(lions_bills.kind, "moneyline")
        self.assertEqual(by_id[900004].kind, "spread")

    def test_camel_case_and_completed(self):
        payload = {"data": [
            {"id": 1, "numberTypes": [{"title": "A", "betPercentage": 30}, {"title": "B", "betPercentage": 70}]},
            {"id": 2, "is_complete": True, "number_types": [{"title": "A", "bet_percentage": 30}, {"title": "B", "bet_percentage": 70}]},
        ]}
        self.assertEqual([m.id for m in parse_markets(payload)], [1])

    def test_polymarket_event_mirrors_quotes(self):
        event = {"title": "Lions vs. Bills", "slug": "nfl-det-buf", "markets": [{
            "sportsMarketType": "moneyline", "outcomes": '["Lions", "Bills"]', "outcomePrices": '["0.325", "0.675"]',
            "bestBid": "0.32", "bestAsk": "0.33", "gameStartTime": "2026-09-18 00:15:00+00", "volumeNum": 10768}]}
        g = PolymarketClient._game_from_event("nfl", event)
        self.assertAlmostEqual(g.outcomes[0].prob, 0.325)
        self.assertAlmostEqual(g.outcomes[1].prob, 0.675)
        self.assertEqual(g.start, THU)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.markets = {m.id: m for m in parse_markets(json.loads((ROOT / "iloveedging/data/slips_sample.json").read_text()))}

    def test_full_names_match_short_outcomes(self):
        m = match_market(self.markets[900001], [game("nfl", "Lions vs. Bills", "Lions", 0.325, "Bills", THU, LIONS, BILLS)])
        self.assertIsNotNone(m)
        self.assertEqual([(s.name, o.name) for s, o in m.pairs], [("Detroit Lions", "Lions"), ("Buffalo Bills", "Bills")])

    def test_reversed_order(self):
        m = match_market(self.markets[900001], [game("nfl", "Bills vs. Lions", "Bills", 0.675, "Lions", THU, BILLS, LIONS)])
        self.assertEqual(m.pairs[0][1].name, "Lions")

    def test_picks_closest_game_in_a_series(self):
        yankees = self.markets[900005]  # 2026-09-15 23:40
        g1 = game("mlb", "Yankees vs Twins", "New York Yankees", 0.62, "Minnesota Twins", datetime(2026, 9, 15, 23, 40, tzinfo=UTC))
        g2 = game("mlb", "Yankees vs Twins 2", "New York Yankees", 0.60, "Minnesota Twins", datetime(2026, 9, 16, 17, 40, tzinfo=UTC))
        self.assertIs(match_market(yankees, [g2, g1]).poly, g1)

    def test_wrong_day_does_not_match(self):
        far = datetime(2026, 9, 25, tzinfo=UTC)
        self.assertIsNone(match_market(self.markets[900001], [game("nfl", "Lions vs. Bills", "Lions", 0.3, "Bills", far, LIONS, BILLS)]))

    def test_scan_flags_underpriced_side(self):
        s = Settings()
        s.edge_threshold, s.min_ev, s.bet_size = 0.08, 0.0, 10
        result = scan([self.markets[900001]], [game("nfl", "Lions vs. Bills", "Lions", 0.325, "Bills", THU, LIONS, BILLS)], s)
        self.assertEqual(len(result.opportunities), 1)
        o = result.opportunities[0]
        self.assertEqual(o.side, "Buffalo Bills")
        self.assertAlmostEqual(o.edge, 0.095)
        # Pari-mutuel: $10 added to $580 of a $1,000 pool returns 10 * 1010 / 590.
        self.assertAlmostEqual(o.win_amount, 10 * 1010 / 590)
        self.assertAlmostEqual(o.fee, 0.5)
        self.assertAlmostEqual(o.ev, (0.675 * 10 * 1010 / 590 - 10.5) / 10.5)
        self.assertEqual(o.opposing_money, 420)


class SlipsRulesTests(unittest.TestCase):
    def pool(self, amounts, names=("Braves", "Cubs"), label="Win"):
        total = sum(amounts)
        return parse_markets({"id": 7, "title": "Braves vs Cubs", "market_type": {"label": label}, "drawing_at": "2026-09-15 22:40:00 +0000",
                              "number_types": [{"id": i, "title": n, "bet_amount": a, "bet_percentage": round(100 * a / total) if total else 0}
                                               for i, (n, a) in enumerate(zip(names, amounts))]})[0]

    def test_kinds(self):
        prop = self.pool([10, 10], ("Over", "Under"), "touchdowns")
        prop.prop_category = "player"
        self.assertEqual(prop.kind, "prop")
        self.assertEqual(self.pool([10, 10], ("Over", "Under"), "Total points").kind, "total")
        self.assertEqual(self.pool([10, 10]).kind, "moneyline")

    def test_spread_and_total_match_polymarket_lines(self):
        start = datetime(2026, 9, 18, 0, 15, tzinfo=UTC)
        spread = PolyMarket("spreads", "Spread: Bills (-4.5)", -4.5, 40_000, 0.01,
                            [PolyOutcome("Bills", 0.505, None, None), PolyOutcome("Lions", 0.495, None, None)])
        other_line = PolyMarket("spreads", "Spread: Bills (-3.5)", -3.5, 40_000, 0.01,
                                [PolyOutcome("Bills", 0.6, None, None), PolyOutcome("Lions", 0.4, None, None)])
        total = PolyMarket("totals", "O/U 53.5", 53.5, 5_000, 0.01,
                           [PolyOutcome("Over", 0.45, None, None), PolyOutcome("Under", 0.55, None, None)])
        g = game("nfl", "Lions vs. Bills", "Lions", 0.335, "Bills", start, LIONS, BILLS, extra=[other_line, spread, total])

        sp = parse_markets({"id": 1, "title": "Lions vs Bills", "sport": "Football", "drawing_at": "2026-09-18 00:15:00 +0000",
                            "market_type": {"label": "Spread"}, "number_types": [
                                {"id": 1, "title": "Bills -4.5", "spread": -4.5, "bet_percentage": 86, "bet_amount": 60},
                                {"id": 2, "title": "Lions +4.5", "spread": 4.5, "bet_percentage": 14, "bet_amount": 10}]})[0]
        m = match_market(sp, [g])
        self.assertIs(m.market, spread)
        self.assertEqual({(s.name, o.name, o.prob) for s, o in m.pairs}, {("Bills -4.5", "Bills", 0.505), ("Lions +4.5", "Lions", 0.495)})

        tot = parse_markets({"id": 2, "title": "Lions vs Bills", "sport": "Football", "drawing_at": "2026-09-18 00:15:00 +0000",
                             "market_type": {"label": "Total points"}, "bet_market_value": "53.5", "number_types": [
                                 {"id": 3, "title": "Over", "bet_percentage": 30, "bet_amount": 30},
                                 {"id": 4, "title": "Under", "bet_percentage": 70, "bet_amount": 70}]})[0]
        self.assertIs(match_market(tot, [g]).market, total)
        tot.line = 49.5
        self.assertIsNone(match_market(tot, [g]))

    def test_empty_and_tiny_pools_are_not_alerted(self):
        start = datetime(2026, 9, 15, 22, 40, tzinfo=UTC)
        g = game("mlb", "Braves vs. Cubs", "Atlanta Braves", 0.44, "Chicago Cubs", start)
        s = Settings()
        empty = scan([self.pool([0, 0])], [g], s)
        self.assertEqual((empty.skipped_empty, empty.opportunities), (1, []))
        tiny = scan([self.pool([10, 0])], [g], s)
        self.assertEqual(tiny.opportunities, [])
        self.assertIn("other side", next(o for o in tiny.all_compared if o.side == "Cubs").skip_reason)

    def test_midnight_placeholder_times_still_match(self):
        m = self.pool([50, 50], ("Houston Cougars", "Texas Tech Red Raiders"))
        m.sport, m.start = "Football", datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
        g = game("cfb", "Houston vs. Texas Tech", "Houston", 0.4, "Texas Tech", datetime(2026, 9, 19, 0, 0, tzinfo=UTC))
        self.assertIsNotNone(match_market(m, [g]))

if __name__ == "__main__":
    unittest.main()
