"""Loopback-only scanner service used internally by the terminal application."""

from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .alerts import Alerter
from .config import STATE_DIR, Settings
from .polymarket import PolyGame
from .scanner import Opportunity, ScanResult, scan
from .slips import SlipsAuthError, SlipsClient, SlipsMarket

UI_SETTINGS = STATE_DIR / "ui_settings.json"
TUNABLE = ("edge_threshold", "min_ev", "bet_size", "min_opposing_money", "min_poly_volume", "max_poly_spread")


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _opp_json(o: Opportunity, qualifies: bool) -> dict:
    s = o.slips
    return {
        "key": o.key,
        "kind": o.kind,
        "pick": o.pick,
        "description": s.description,
        "title": s.title or o.poly.title,
        "sport": s.sport,
        "league": o.poly.league,
        "start": _iso(s.start),
        "is_live": s.is_live,
        "slips_prob": o.slips_prob,
        "poly_prob": o.poly_prob,
        "edge": o.edge,
        "ev": o.ev,
        "stake": o.stake,
        "fee": o.fee,
        "win_amount": o.win_amount,
        "payout": o.payout,
        "opposing_money": o.opposing_money,
        "pool_total": s.pool_total,
        "already_bet": o.already_bet,
        "skip_reason": o.skip_reason,
        "qualifies": qualifies,
        "slips_url": s.url,
        "poly_url": o.poly.url,
        "poly_title": o.poly.title,
        "poly_question": o.market.question,
        "poly_volume": o.market.volume,
        "poly_spread": o.market.spread,
        "slips_sides": [{"name": x.name, "pct": x.percentage, "amount": x.bet_amount} for x in s.sides],
    }


class Engine:
    def __init__(self, settings: Settings, interval: int):
        self.settings = settings
        self.interval = interval
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.markets: list[SlipsMarket] = []
        self.games: list[PolyGame] = []
        self.result: ScanResult | None = None
        self.feeds: dict[str, str] = {}
        self.error = ""
        self.scanning = False
        self.last_scan: datetime | None = None
        self.next_scan: float = 0
        self.history: list[dict] = []  # small time series for the header sparkline
        self._load_ui_settings()

    def _load_ui_settings(self) -> None:
        try:
            saved = json.loads(UI_SETTINGS.read_text())
        except (OSError, ValueError):
            return
        for k in TUNABLE:
            if isinstance(saved.get(k), (int, float)):
                setattr(self.settings, k, float(saved[k]))

    def update_settings(self, values: dict) -> None:
        with self.lock:
            for k in TUNABLE:
                if isinstance(values.get(k), (int, float)):
                    setattr(self.settings, k, float(values[k]))
            UI_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            UI_SETTINGS.write_text(json.dumps({k: getattr(self.settings, k) for k in TUNABLE}))
            if self.markets:
                self.result = scan(self.markets, self.games, self.settings)

    def run_forever(self) -> None:
        from .__main__ import load_poly_games  # avoid a circular import at module load

        client = None
        while True:
            self.scanning = True
            try:
                client = client or SlipsClient(self.settings.slips_access_token, self.settings.slips_refresh_token)
                markets, feeds = client.fetch_feeds()
                games = load_poly_games(self.settings)
                with self.lock:
                    self.markets, self.games, self.feeds = markets, games, feeds
                    self.result = scan(markets, games, self.settings)
                    self.last_scan = datetime.now(timezone.utc)
                    self.error = ""
                    best = max((o.ev for o in self.result.opportunities), default=0)
                    self.history = (self.history + [{"t": _iso(self.last_scan), "count": len(self.result.opportunities), "best_ev": best}])[-48:]
                alerter = Alerter(self.settings)
                alerter.send(alerter.new_only(self.result.opportunities))
            except SlipsAuthError as e:
                self.error = str(e)
                client = None
            except Exception as e:  # keep serving the last good data
                self.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                self.scanning = False
            self.next_scan = time.time() + self.interval
            self.wake.wait(self.interval)
            self.wake.clear()

    def state(self) -> dict:
        with self.lock:
            r = self.result
            hits = {o.key for o in r.opportunities} if r else set()
            return {
                "scanning": self.scanning,
                "error": self.error,
                "last_scan": _iso(self.last_scan),
                "next_scan": datetime.fromtimestamp(self.next_scan, timezone.utc).isoformat() if self.next_scan else None,
                "interval": self.interval,
                "feeds": self.feeds,
                "settings": {k: getattr(self.settings, k) for k in TUNABLE},
                "counts": {
                    "slips_pools": len(self.markets),
                    "polymarket_games": len(self.games),
                    "compared": len(r.all_compared) if r else 0,
                    "empty": r.skipped_empty if r else 0,
                    "unmatched": len(r.unmatched) if r else 0,
                },
                "opportunities": [_opp_json(o, True) for o in r.opportunities] if r else [],
                "compared": [_opp_json(o, o.key in hits) for o in r.all_compared] if r else [],
                "unmatched": [
                    {"id": m.id, "title": m.title, "description": m.description, "kind": m.kind, "sport": m.sport,
                     "pool_total": m.pool_total, "start": _iso(m.start), "url": m.url,
                     "sides": [{"name": x.name, "pct": x.percentage, "amount": x.bet_amount} for x in m.sides]}
                    for m in r.unmatched
                ] if r else [],
                "history": self.history,
            }


def make_handler(engine: Engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                return self._json(engine.state())
            self._json({"error": "No web interface. Run iloveedging in your terminal."}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            if path == "/api/scan":
                engine.wake.set()
                return self._json({"ok": True})
            if path == "/api/settings":
                engine.update_settings(body)
                return self._json(engine.state())
            self._json({"error": "not found"}, 404)

    return Handler


def serve(settings: Settings, port: int, interval: int) -> None:
    engine = Engine(settings, interval)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine))
    except OSError as e:
        if e.errno == 48 or "in use" in str(e).lower():
            print(f"Port {port} is already in use. Connect with iloveedging --port {port}, "
                  f"or use another port: iloveedging --port {port + 1}")
            raise SystemExit(1)
        raise
    threading.Thread(target=engine.run_forever, daemon=True).start()
    print(f"iloveedging scanner listening on 127.0.0.1:{port} (Ctrl+C to stop; no browser UI)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
