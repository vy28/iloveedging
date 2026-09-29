"""iloveedging's native, read-only terminal workspace."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static, Tab, Tabs

from .config import ROOT

LIME = "#c8ff79"
CYAN = "#7edce5"
MUTED = "#8b97a8"
AMBER = "#f4bc78"
RED = "#ff8f9c"

RULES = (
    ("bet_size", "Modeled stake · $", 1, 0.01, None),
    ("edge_threshold", "Minimum edge · percentage points", 100, 0, 100),
    ("min_ev", "Minimum expected return · %", 100, 0, None),
    ("min_opposing_money", "Minimum opposing pool · $", 1, 0, None),
    ("min_poly_volume", "Minimum Poly volume · $", 1, 0, None),
    ("max_poly_spread", "Maximum Poly bid/ask spread · points", 100, 0, 100),
)


class BackendError(Exception):
    pass


class LocalBackend:
    def __init__(self, port: int, interval: int, connect_only: bool = False):
        self.url = f"http://127.0.0.1:{port}"
        self.port = port
        self.interval = interval
        self.connect_only = connect_only
        self.process: subprocess.Popen | None = None

    def request(self, path: str, values: dict | None = None) -> dict:
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(
                "GET" if values is None else "POST", self.url + path,
                json=values, timeout=(1, 5), allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise BackendError("The local service returned an unexpected response.")
        return payload

    def fetch(self) -> dict:
        state = self.request("/api/state")
        if not all(isinstance(state.get(key), kind) for key, kind in (
            ("compared", list), ("unmatched", list), ("settings", dict), ("counts", dict),
        )):
            raise BackendError("This port is not serving a Slips scanner. Try --port 8788.")
        return state

    def start(self) -> None:
        try:
            self.fetch()
        except requests.ConnectionError:
            if self.connect_only:
                return
            environment = dict(os.environ)
            environment.update(DISCORD_WEBHOOK_URL="", TELEGRAM_BOT_TOKEN="", MACOS_NOTIFICATIONS="0")
            self.process = subprocess.Popen(
                [sys.executable, "-m", "iloveedging", "backend", "--port", str(self.port),
                 "--interval", str(self.interval)],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )

    def rescan(self) -> dict:
        return self.request("/api/scan", {})

    def save(self, values: dict) -> dict:
        return self.request("/api/settings", values)

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def local_time(value: str | None) -> str:
    if not value:
        return "Awaiting first scan"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%b %d · %I:%M %p")
    except ValueError:
        return "Time unavailable"


def visible_rows(state: dict, view: str, min_probability: float, query: str, sort: str) -> list[dict]:
    rows = state.get("unmatched" if view == "unmatched" else "compared", [])
    if view == "opportunities":
        rows = [row for row in rows if row.get("qualifies")]
    if view != "unmatched":
        rows = [row for row in rows if row.get("poly_prob", 0) >= min_probability]
    words = query.casefold().split()
    rows = [row for row in rows if all(word in " ".join(str(row.get(key, "")) for key in (
        "title", "pick", "description", "sport", "league", "kind",
    )).casefold() for word in words)]
    if view == "unmatched":
        return sorted(rows, key=lambda row: (row.get("title", ""), str(row["id"])))
    return sorted(rows, key=lambda row: (-row.get(sort, 0), str(row["key"])))


def parse_rules(values: dict[str, str]) -> dict[str, float]:
    result = {}
    for key, label, scale, minimum, maximum in RULES:
        try:
            value = float(values[key])
        except (KeyError, ValueError):
            raise ValueError(f"{label}: enter a number.") from None
        if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
            limit = f"{minimum:g}–{maximum:g}" if maximum is not None else f"at least {minimum:g}"
            raise ValueError(f"{label}: use {limit}.")
        result[key] = value / scale
    return result


def detail_text(row: dict | None, unmatched: bool = False) -> Text:
    content = Text()
    if row is None:
        content.append("Nothing selected\n\n", style="bold")
        content.append("Select a row to inspect the market, pricing and modeled return.\n\nNo bets are placed here.", style=MUTED)
        return content
    content.append(str(row.get("league") or row.get("sport") or "SPORT").upper(), style=CYAN)
    content.append(f"  /  {row.get('kind', 'market').upper()}\n\n", style=MUTED)
    content.append(f"{row.get('pick') or row.get('title')}\n", style="bold")
    if row.get("pick"):
        content.append(f"{row.get('title', '')}\n", style=MUTED)
    content.append(f"{row.get('description', '')}\n", style=MUTED)
    content.append(f"{local_time(row.get('start'))}\n\n", style=MUTED)
    if unmatched:
        content.append("NO EQUIVALENT MARKET\n", style=AMBER)
        content.append("No matching Polymarket line was found. A Poly probability or edge cannot be inferred.\n\n")
        for side in row.get("sides", []):
            content.append(f"{side['name']}  {side['pct']:g}%\n")
        content.append(f"\nSlips pool   {money(row.get('pool_total'))}\n")
    else:
        for label, probability, color in (
            ("POLY ", row["poly_prob"], CYAN), ("SLIPS", row["slips_prob"], LIME),
        ):
            filled = round(max(0, min(1, probability)) * 18)
            content.append(f"{label}  ", style=MUTED)
            content.append("━" * filled, style=color)
            content.append("━" * (18 - filled), style="#303a48")
            content.append(f"  {probability:.1%}\n", style=f"bold {color}")
        content.append(f"\n{row['edge'] * 100:+.1f} pts edge", style=LIME if row["edge"] > 0 else RED)
        content.append(f"   {row['ev']:+.1%} EV\n\n", style=LIME if row["ev"] > 0 else RED)
        content.append("MODELED PAYOUT\n", style=f"bold {MUTED}")
        cost = row["stake"] + row["fee"]
        for label, value in (
            ("Stake", money(row["stake"])), ("Fee", money(row["fee"])),
            ("Total cost / max loss", money(cost)), ("Return if win", money(row["win_amount"])),
            ("Net profit if win", money(row["win_amount"] - cost)),
            ("Expected net profit", money(row["ev"] * cost)),
        ):
            content.append(f"{label:<23}{value}\n")
        content.append("\nSCAN VERDICT\n", style=f"bold {MUTED}")
        if row.get("qualifies"):
            content.append("● Passes your scanner rules\n", style=LIME)
        else:
            content.append(f"○ {row.get('skip_reason') or 'Below your edge / EV thresholds'}\n", style=AMBER)
        if row.get("already_bet"):
            content.append("You already have a position in this pool.\n", style=AMBER)
        content.append(f"\nOpposing pool   {money(row.get('opposing_money'))}\n", style=MUTED)
        content.append(f"Poly volume     {money(row.get('poly_volume'))}\n", style=MUTED)
        spread = row.get("poly_spread")
        content.append(f"Bid/ask spread  {spread * 100:.1f} pts\n" if spread is not None else "Bid/ask spread  —\n", style=MUTED)
        content.append(f"\nMatched line\n{row.get('poly_question', '')}\n", style=MUTED)
    content.append("\nPrices are market estimates, not guaranteed chances. Pools and payouts can change. Verify the exact line before betting.", style=MUTED)
    return content


class DetailScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Back")]

    def __init__(self, content: Text):
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-dialog"):
            yield Label("MARKET INSPECTOR", classes="dialog-title")
            with VerticalScroll():
                yield Static(self.content)
            yield Button("Back  ·  Esc", id="close-detail")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class ProbabilityScreen(ModalScreen[float | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, minimum: float):
        super().__init__()
        self.minimum = minimum

    def compose(self) -> ComposeResult:
        with Vertical(id="probability-dialog"):
            yield Label("MINIMUM POLY PROBABILITY", classes="dialog-title")
            yield Static("Enter 0–100%. Decimals accepted; 0 shows all.\nApplies to this terminal session, not scanner alerts.", classes="muted")
            yield Input(f"{self.minimum * 100:g}", id="minimum-probability")
            yield Static("", id="probability-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-probability")
                yield Button("Apply filter", id="apply-probability", variant="primary")

    def on_mount(self) -> None:
        field = self.query_one(Input)
        field.focus()
        field.action_select_all()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def apply_filter(self) -> None:
        try:
            percentage = float(self.query_one(Input).value)
            if not math.isfinite(percentage) or not 0 <= percentage <= 100:
                raise ValueError
        except ValueError:
            self.query_one("#probability-error", Static).update("Enter a valid percentage between 0 and 100.")
            return
        self.dismiss(percentage / 100)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.apply_filter()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cancel-probability":
            self.action_cancel()
        else:
            self.apply_filter()


class RulesScreen(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, settings: dict):
        super().__init__()
        self.settings = settings
        self.saving = False

    def compose(self) -> ComposeResult:
        with Vertical(id="rules-dialog"):
            yield Label("TUNE YOUR SCANNER", classes="dialog-title")
            yield Static("Rescores current data. Saved locally for future scans.", classes="muted")
            with VerticalScroll(id="rule-fields"):
                for key, label, scale, minimum, maximum in RULES:
                    yield Label(label)
                    yield Input(f"{self.settings.get(key, 0) * scale:g}", id=key)
            yield Static("", id="rule-error", markup=False)
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="cancel-rules")
                yield Button("Apply rules", id="save-rules", variant="primary")

    def action_cancel(self) -> None:
        if not self.saving:
            self.dismiss()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-rules":
            self.action_cancel()
            return
        if self.saving:
            return
        try:
            values = parse_rules({key: self.query_one(f"#{key}", Input).value for key, *_ in RULES})
        except ValueError as error:
            self.query_one("#rule-error", Static).update(str(error))
            return
        self.saving = True
        event.button.disabled = True
        try:
            await asyncio.to_thread(self.app.backend.save, values)
        except (requests.RequestException, ValueError, BackendError, OSError):
            self.query_one("#rule-error", Static).update("Could not confirm the save. Reconnect and check your rules.")
        else:
            self.app.notify("Rules saved. Current pools rescored.", title="Scanner updated")
            self.dismiss()
            self.app.refresh_data()
        finally:
            self.saving = False
            event.button.disabled = False


class EdgeTerminal(App):
    TITLE = "iloveedging · Slips × Polymarket"
    CSS_PATH = "terminal.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("1", "view('opportunities')", "Picks", show=False),
        Binding("2", "view('markets')", "Markets", show=False),
        Binding("3", "view('unmatched')", "Unmatched", show=False),
        Binding("slash", "search", "Search"),
        Binding("f", "probability", "Min Poly %"),
        Binding("s", "sort", "Sort"),
        Binding("r", "rescan", "Scan"),
        Binding("e", "rules", "Rules"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, backend: LocalBackend):
        super().__init__()
        self.backend = backend
        self.data: dict = {}
        self.view = "opportunities"
        self.min_poly_probability = .62
        self.sort_field = "ev"
        self.selected_key: str | None = None
        self.rows: dict[str, dict] = {}
        self.connected = False
        self.has_polled = False
        self.polling = False
        self.scan_pending = False
        self.table_signature = None
        self.table_view = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="masthead"):
            yield Static(Text.assemble(("◈ iloveedging", f"bold {LIME}"), ("  /  SLIPS × POLYMARKET", MUTED)), id="brand")
            yield Static("○ CONNECTING", id="connection", markup=False)
        with Horizontal(id="intro"):
            yield Static("Your edge. In focus.", id="headline")
            yield Static("MARKET INTELLIGENCE  /  READ ONLY", id="read-only")
        with Horizontal(id="metrics"):
            for name in ("pools", "compared", "qualified", "best"):
                yield Static("—", id=f"metric-{name}", classes="metric")
        yield Tabs(Tab("01  Opportunities", id="opportunities"), Tab("02  All markets", id="markets"),
                   Tab("03  Unmatched", id="unmatched"), id="views")
        with Horizontal(id="toolbar"):
            yield Input(placeholder="Search team, league or market…  /", id="search")
            yield Button(f"Poly ≥{self.min_poly_probability * 100:g}%", id="probability", classes="filter-on")
            yield Button("Sort: EV ↓", id="sort")
        with Horizontal(id="workspace"):
            with Vertical(id="market-panel"):
                yield Static("OPPORTUNITY FEED", id="list-heading")
                yield DataTable(id="markets-table", cursor_type="row", zebra_stripes=True)
                yield Static("Connecting to your local scanner…", id="empty", markup=False)
                yield Static("↑ ↓ navigate   Enter inspect   1 / 2 / 3 switch views", id="table-hint")
            with VerticalScroll(id="inspector"):
                yield Static("MARKET INSPECTOR", classes="section-label")
                yield Static(detail_text(None), id="details")
        yield Static("Starting scanner…", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.query_one(DataTable).focus()
        self.update_layout(self.size.width, self.size.height)
        self.render_data()
        self.refresh_data()
        self.set_interval(3, self.refresh_data)
        self.set_interval(1, self.render_status)

    def on_resize(self, event) -> None:
        self.update_layout(event.size.width, event.size.height)

    def update_layout(self, width: int, height: int) -> None:
        self.set_class(width < 130 or height < 32, "compact")
        self.set_class(width < 90, "narrow")
        self.set_class(height < 28, "short")

    @work(group="poll")
    async def refresh_data(self) -> None:
        if self.polling:
            return
        self.polling = True
        try:
            self.data = await asyncio.to_thread(self.backend.fetch)
            self.connected = True
        except (requests.RequestException, ValueError, BackendError, OSError):
            self.connected = False
        finally:
            self.polling = False
            self.has_polled = True
        self.render_data()

    def render_status(self) -> None:
        if not self.is_mounted:
            return
        if not self.has_polled:
            title, color = "◌ CONNECTING", CYAN
            message = "Connecting to your local scanner…"
        elif not self.connected:
            title, color = "○ OFFLINE", RED
            message = f"Scanner unavailable on port {self.backend.port}. Retrying…"
            if self.data.get("last_scan"):
                message += "  Last snapshot retained."
        elif self.data.get("error"):
            title, color = "! SCAN ERROR", AMBER
            message = "Scan failed · showing last good snapshot. Check Slips tokens / network; R to retry."
        elif self.data.get("scanning"):
            title, color = "◌ SCANNING", CYAN
            message = "Fetching Slips pools and Polymarket lines…  You can keep browsing."
        elif not self.data.get("last_scan"):
            title, color = "◌ WARMING UP", CYAN
            message = "Connected · waiting for the first complete scan."
        else:
            stamp = datetime.fromisoformat(self.data["last_scan"])
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            stale = age > max(60, self.data.get("interval", 120) * 2 + 30)
            title, color = ("○ STALE", AMBER) if stale else ("● CONNECTED", LIME)
            message = f"Last scan {local_time(self.data['last_scan'])} · {int(max(0, age))}s ago"
            if self.data.get("next_scan"):
                remaining = max(0, int((datetime.fromisoformat(self.data["next_scan"]) - datetime.now(timezone.utc)).total_seconds()))
                message += f" · next in {remaining}s"
            message += "  |  Market estimates, not guarantees."
        self.query_one("#connection", Static).update(Text(title, style=f"bold {color}"))
        self.query_one("#status", Static).update(Text(message, style=color if color in (RED, AMBER) else MUTED))

    def render_data(self) -> None:
        query = self.query_one("#search", Input).value
        rows = visible_rows(self.data, self.view, self.min_poly_probability, query, self.sort_field)
        picks = visible_rows(self.data, "opportunities", self.min_poly_probability, "", "ev")
        counts = self.data.get("counts", {})
        values = (
            ("pools", "SLIPS POOLS", str(counts.get("slips_pools", "—")), MUTED),
            ("compared", "COMPARED SIDES", str(counts.get("compared", "—")), CYAN),
            ("qualified", f"QUALIFIED · {self.min_poly_probability * 100:g}%+" if self.min_poly_probability else "QUALIFIED · ALL", str(len(picks)) if self.data else "—", LIME),
            ("best", "BEST QUALIFIED EV", f"{picks[0]['ev']:+.1%}" if picks else "—", LIME),
        )
        for name, label, value, color in values:
            self.query_one(f"#metric-{name}", Static).update(Text.assemble((label + "\n", MUTED), (value, f"bold {color}")))
        self.query_one("#probability", Button).disabled = self.view == "unmatched"
        self.query_one("#sort", Button).disabled = self.view == "unmatched"
        heading = {"opportunities": "QUALIFIED OPPORTUNITIES", "markets": "ALL COMPARED SIDES", "unmatched": "UNMATCHED POOLS"}[self.view]
        self.query_one("#list-heading", Static).update(Text.assemble((heading, "bold"), (f"  /  {len(rows)} shown", MUTED)))
        signature = (self.view, rows)
        table = self.query_one(DataTable)
        if signature != self.table_signature:
            same_view = self.table_view == self.view
            old_cursor = table.cursor_row if same_view else 0
            table.clear(columns=not same_view)
            if not same_view:
                if self.view == "unmatched":
                    columns = (("GAME / POOL", 35), ("SPORT", 7), ("TYPE", 10), ("POOL", 12))
                else:
                    columns = (("PICK / GAME", 22), ("LEAGUE", 6), ("POLY", 6), ("SLIPS", 6),
                               ("EDGE", 7), ("EV", 7), ("RULE", 4))
                for label, width in columns:
                    table.add_column(label, width=width)
            self.rows = {}
            for row in rows:
                key = str(row["id"] if self.view == "unmatched" else row["key"])
                self.rows[key] = row
                if self.view == "unmatched":
                    cells = [str(row.get("title", ""))[:35], str(row.get("sport", "")).upper(),
                             str(row.get("kind", "")), money(row.get("pool_total"))]
                else:
                    pick = Text.assemble((str(row["pick"]), "bold"), (f"\n{row.get('title', '')}", MUTED))
                    cells = [pick, str(row.get("league", "")).upper(),
                             Text(f"{row['poly_prob']:.1%}", style=CYAN), f"{row['slips_prob']:.1%}",
                             Text(f"{row['edge'] * 100:+.1f}p", style=LIME if row["edge"] > 0 else RED),
                             Text(f"{row['ev']:+.1%}", style=LIME if row["ev"] > 0 else RED),
                             Text("PASS" if row.get("qualifies") else "SKIP", style=LIME if row.get("qualifies") else MUTED)]
                table.add_row(*(Text(cell) if isinstance(cell, str) else cell for cell in cells), key=key,
                              height=1 if self.view == "unmatched" else 2)
            keys = list(self.rows)
            position = keys.index(self.selected_key) if same_view and self.selected_key in keys else min(old_cursor, max(0, len(keys) - 1))
            self.selected_key = keys[position] if keys else None
            if keys:
                table.move_cursor(row=position)
            self.table_signature = signature
            self.table_view = self.view
        table.display = bool(rows)
        empty = self.query_one("#empty", Static)
        empty.display = not rows
        if not self.data or not self.data.get("last_scan"):
            message = "◌  Waiting for market data\n\nThe first scan can take a minute. Check the status below."
        elif self.view == "unmatched":
            message = "No unmatched pools here.\n\nTry clearing your search."
        else:
            message = "No markets pass this view.\n\nTry All markets (2), clear search (Esc), or change the minimum Poly percentage (F).\nNo qualifying edge is a valid result."
        empty.update(message)
        self.show_selection()
        self.render_status()

    def show_selection(self) -> None:
        self.query_one("#details", Static).update(detail_text(self.rows.get(self.selected_key), self.view == "unmatched"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key.value in self.rows:
            self.selected_key = event.row_key.value
            self.show_selection()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = self.rows.get(event.row_key.value)
        if row:
            self.push_screen(DetailScreen(detail_text(row, self.view == "unmatched")))

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        self.view = event.tab.id
        self.render_data()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.render_data()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one(DataTable).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "probability":
            self.action_probability()
        elif event.button.id == "sort":
            self.action_sort()

    def action_view(self, view: str) -> None:
        self.query_one("#views", Tabs).active = view
        self.query_one(DataTable).focus()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        self.query_one("#search", Input).value = ""
        self.query_one(DataTable).focus()

    def action_probability(self) -> None:
        if self.view == "unmatched":
            return
        self.push_screen(ProbabilityScreen(self.min_poly_probability), self.set_min_probability)

    def set_min_probability(self, minimum: float | None) -> None:
        if minimum is None:
            return
        self.min_poly_probability = minimum
        button = self.query_one("#probability", Button)
        button.label = f"Poly ≥{minimum * 100:g}%" if minimum else "Poly: all"
        button.set_class(minimum > 0, "filter-on")
        self.render_data()
        self.query_one(DataTable).focus()

    def action_sort(self) -> None:
        if self.view == "unmatched":
            return
        fields = ("ev", "poly_prob", "edge")
        self.sort_field = fields[(fields.index(self.sort_field) + 1) % len(fields)]
        label = {"ev": "EV", "poly_prob": "Poly", "edge": "Edge"}[self.sort_field]
        self.query_one("#sort", Button).label = f"Sort: {label} ↓"
        self.render_data()

    @work(group="rescan")
    async def action_rescan(self) -> None:
        if self.data.get("scanning") or self.scan_pending:
            self.notify("A scan is already running or queued.")
            return
        self.scan_pending = True
        try:
            await asyncio.to_thread(self.backend.rescan)
        except (requests.RequestException, ValueError, BackendError, OSError):
            self.notify("Could not request a scan. Check the local server.", severity="error")
        else:
            self.notify("Fresh scan requested.", title="Scanner")
            self.refresh_data()
        finally:
            self.scan_pending = False

    def action_rules(self) -> None:
        if not self.connected or not self.data.get("settings"):
            self.notify("Connect to the scanner before editing rules.", severity="warning")
            return
        self.push_screen(RulesScreen(self.data["settings"]))


def run_terminal(port: int, interval: int, connect_only: bool) -> int:
    if not 1 <= port <= 65535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    backend = LocalBackend(port, interval, connect_only)
    try:
        backend.start()
        EdgeTerminal(backend).run()
    except BackendError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (requests.RequestException, ValueError, OSError):
        print("Could not start the terminal dashboard. Check the local server / port or try --port 8788.", file=sys.stderr)
        return 2
    finally:
        backend.close()
    return 0
