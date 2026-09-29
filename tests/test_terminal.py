"""Offline terminal UI and backend ownership tests."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests
from textual.widgets import Button, DataTable, Input, Static

from iloveedging.config import Settings
from iloveedging.terminal import (
    RULES, BackendError, DetailScreen, EdgeTerminal, LocalBackend, ProbabilityScreen, RulesScreen,
    detail_text, parse_rules, visible_rows,
)


def sample_state():
    now = datetime.now(timezone.utc)
    first = {
        "key": "bills", "pick": "Buffalo Bills", "title": "Bills vs Lions", "league": "nfl",
        "kind": "moneyline", "description": "Full game winner", "start": now.isoformat(),
        "poly_prob": .62, "slips_prob": .42, "edge": .20, "ev": .3, "stake": 10,
        "fee": .5, "win_amount": 22.02, "opposing_money": 100, "pool_total": 200,
        "poly_volume": 15000, "poly_spread": .02, "qualifies": True,
        "poly_question": "Will Buffalo win?", "already_bet": False,
    }
    return {
        "compared": [
            first,
            {**first, "key": "rangers", "pick": "Rangers +1.5", "title": "Rangers vs Bruins",
             "league": "nhl", "kind": "spread", "poly_prob": .75, "ev": .2},
            {**first, "key": "under", "pick": "Under 8.5", "poly_prob": .619, "ev": .5},
            {**first, "key": "skip", "pick": "Yankees", "poly_prob": .80, "qualifies": False,
             "skip_reason": "Not enough opposing money"},
        ],
        "unmatched": [{"id": 17, "title": "Unmatched fixture", "kind": "prop", "sport": "MLB",
                       "pool_total": 50, "sides": []}],
        "counts": {"slips_pools": 20, "compared": 4},
        "settings": {key: getattr(Settings(), key) for key, *_ in RULES},
        "last_scan": now.isoformat(), "next_scan": (now + timedelta(seconds=120)).isoformat(),
        "interval": 120, "scanning": False, "error": "",
    }


class FakeBackend:
    port = 8787

    def __init__(self):
        self.state = sample_state()
        self.offline = False
        self.scans = 0
        self.saved = None

    def fetch(self):
        if self.offline:
            raise requests.ConnectionError("offline")
        return copy.deepcopy(self.state)

    def rescan(self):
        self.scans += 1

    def save(self, values):
        if self.offline:
            raise requests.ConnectionError("offline")
        self.saved = values
        self.state["settings"].update(values)
        return self.fetch()


class FilterTests(unittest.TestCase):
    def test_probability_boundary_and_qualified_only(self):
        rows = visible_rows(sample_state(), "opportunities", .62, "", "ev")
        self.assertEqual([row["key"] for row in rows], ["bills", "rangers"])

    def test_search_sort_and_all_markets(self):
        rows = visible_rows(sample_state(), "markets", .62, "nfl bills", "poly_prob")
        self.assertEqual([row["key"] for row in rows], ["skip", "bills"])
        self.assertEqual(len(visible_rows(sample_state(), "opportunities", 0, "", "ev")), 3)

    def test_custom_probability_threshold(self):
        rows = visible_rows(sample_state(), "opportunities", .75, "", "ev")
        self.assertEqual([row["key"] for row in rows], ["rangers"])
        self.assertEqual(len(visible_rows(sample_state(), "opportunities", .619, "", "ev")), 3)
        self.assertEqual(visible_rows(sample_state(), "markets", 1, "", "ev"), [])

    def test_unmatched_does_not_invent_probability(self):
        rows = visible_rows(sample_state(), "unmatched", .75, "", "ev")
        self.assertEqual(len(rows), 1)
        self.assertIn("cannot be inferred", detail_text(rows[0], True).plain)

    def test_payout_is_return_not_profit(self):
        content = detail_text(sample_state()["compared"][0]).plain
        self.assertIn("Total cost / max loss  $10.50", content)
        self.assertIn("Return if win          $22.02", content)
        self.assertIn("Net profit if win      $11.52", content)
        self.assertIn("Expected net profit    $3.15", content)

    def test_rules_units_and_validation(self):
        values = {key: str(getattr(Settings(), key) * scale) for key, _, scale, *_ in RULES}
        self.assertEqual(parse_rules(values)["edge_threshold"], .08)
        for key, value in (("bet_size", "0"), ("min_ev", "nan"), ("min_poly_volume", "inf"),
                           ("max_poly_spread", "101"), ("edge_threshold", "-1"), ("bet_size", "")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                parse_rules({**values, key: value})


class BackendTests(unittest.TestCase):
    def test_existing_backend_is_never_started_or_stopped(self):
        backend = LocalBackend(8787, 120)
        with patch.object(backend, "fetch", return_value=sample_state()), patch("iloveedging.terminal.subprocess.Popen") as process:
            backend.start()
            backend.close()
            process.assert_not_called()

    def test_connect_only_never_starts_backend(self):
        backend = LocalBackend(8787, 120, connect_only=True)
        with patch.object(backend, "fetch", side_effect=requests.ConnectionError), patch("iloveedging.terminal.subprocess.Popen") as process:
            backend.start()
            process.assert_not_called()

    def test_only_owned_child_is_terminated(self):
        backend = LocalBackend(8788, 90)
        child = Mock()
        child.poll.return_value = None
        with patch.object(backend, "fetch", side_effect=requests.ConnectionError), patch("iloveedging.terminal.subprocess.Popen", return_value=child) as process:
            backend.start()
            self.assertIn("8788", process.call_args.args[0])
            self.assertEqual(process.call_args.kwargs["env"]["DISCORD_WEBHOOK_URL"], "")
            backend.close()
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=3)

    def test_rejects_unrelated_local_service(self):
        backend = LocalBackend(8787, 120)
        with patch.object(backend, "request", return_value={"ok": True}), self.assertRaises(BackendError):
            backend.fetch()


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_filter_search_and_inspector(self):
        app = EdgeTerminal(FakeBackend())
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 2)
            await pilot.press("down", "enter")
            self.assertIsInstance(app.screen, DetailScreen)
            await pilot.press("escape", "2")
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 3)
            await pilot.press("f")
            app.screen.query_one("#minimum-probability", Input).value = "0"
            await pilot.press("enter")
            self.assertEqual(app.query_one(DataTable).row_count, 4)
            await pilot.press("slash", "r", "a", "n", "g", "e", "r", "s")
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            self.assertEqual(app.backend.scans, 0)
            await pilot.press("escape", "3")
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            self.assertEqual(app.view, "unmatched")

    async def test_custom_probability_editor_and_cancel(self):
        app = EdgeTerminal(FakeBackend())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#probability")
            self.assertIsInstance(app.screen, ProbabilityScreen)
            await pilot.press("7", "5", "enter")
            self.assertEqual(app.min_poly_probability, .75)
            self.assertEqual(list(app.rows), ["rangers"])
            self.assertIn("75%", str(app.query_one("#probability", Button).label))
            self.assertIn("75%", str(app.query_one("#metric-qualified", Static).render()))
            app.query_one(DataTable).focus()
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(set(app.rows), {"rangers", "skip"})
            app.refresh_data()
            await app.workers.wait_for_complete()
            self.assertEqual(app.min_poly_probability, .75)
            await pilot.press("f")
            app.screen.query_one(Input).value = "10"
            await pilot.press("escape")
            self.assertEqual(app.min_poly_probability, .75)
            await pilot.press("f")
            app.screen.query_one(Input).value = "61.9"
            await pilot.click("#apply-probability")
            self.assertAlmostEqual(app.min_poly_probability, .619)
            self.assertEqual(len(app.rows), 4)
            self.assertIsNone(app.backend.saved)

    async def test_probability_editor_rejects_invalid_values(self):
        app = EdgeTerminal(FakeBackend())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("f")
            for value in ("", "abc", "-1", "101", "nan", "inf"):
                app.screen.query_one(Input).value = value
                await pilot.press("enter")
                self.assertIsInstance(app.screen, ProbabilityScreen)
                self.assertEqual(app.min_poly_probability, .62)
                self.assertIn("0", str(app.screen.query_one("#probability-error", Static).render()))
            app.screen.query_one(Input).value = "100"
            await pilot.press("enter")
            self.assertEqual(app.min_poly_probability, 1)
            self.assertEqual(len(app.rows), 0)

    async def test_poll_preserves_selection_and_recovers_offline(self):
        backend = FakeBackend()
        app = EdgeTerminal(backend)
        async with app.run_test(size=(140, 44)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            self.assertEqual(app.selected_key, "rangers")
            backend.state["compared"][1]["ev"] = .9
            app.refresh_data()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(app.selected_key, "rangers")
            self.assertEqual(app.query_one(DataTable).cursor_row, 0)
            backend.offline = True
            app.refresh_data()
            await app.workers.wait_for_complete()
            self.assertFalse(app.connected)
            self.assertEqual(len(app.rows), 2)
            backend.offline = False
            app.refresh_data()
            await app.workers.wait_for_complete()
            self.assertTrue(app.connected)

    async def test_rules_save_and_poll_while_modal_open(self):
        backend = FakeBackend()
        app = EdgeTerminal(backend)
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            self.assertIsInstance(app.screen, RulesScreen)
            app.refresh_data()
            await app.workers.wait_for_complete()
            app.screen.query_one("#bet_size", Input).value = "25"
            await pilot.click("#save-rules")
            await pilot.pause()
            self.assertEqual(backend.saved["bet_size"], 25)
            self.assertNotIsInstance(app.screen, RulesScreen)
            await pilot.press("r")
            await app.workers.wait_for_complete()
            self.assertEqual(backend.scans, 1)

    async def test_empty_and_small_terminal(self):
        backend = FakeBackend()
        backend.state["compared"] = []
        app = EdgeTerminal(backend)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.has_class("compact"))
            self.assertFalse(app.query_one("#inspector").display)
            self.assertTrue(app.query_one("#empty").display)
            self.assertGreater(app.query_one("#workspace").size.height, 3)
            await pilot.press("slash", "x", "escape")
            self.assertEqual(app.query_one("#search", Input).value, "")

    async def test_resize_switches_between_split_and_compact_layout(self):
        app = EdgeTerminal(FakeBackend())
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#inspector").display)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertFalse(app.query_one("#inspector").display)
            self.assertFalse(app.query_one("#intro").display)
            self.assertGreaterEqual(app.query_one("#workspace").size.height, 10)
            self.assertEqual(app.query_one(DataTable).max_scroll_x, 0)
            await pilot.resize_terminal(150, 48)
            await pilot.pause()
            self.assertTrue(app.query_one("#inspector").display)

    async def test_stale_and_scan_errors_are_not_live(self):
        backend = FakeBackend()
        backend.state["last_scan"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        app = EdgeTerminal(backend)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn("STALE", str(app.query_one("#connection", Static).render()))
            backend.state["error"] = "authentication failed"
            app.refresh_data()
            await app.workers.wait_for_complete()
            self.assertIn("SCAN ERROR", str(app.query_one("#connection", Static).render()))


if __name__ == "__main__":
    unittest.main()
