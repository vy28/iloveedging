"""Command line entry point: iloveedging or python -m iloveedging."""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib.resources import files

import requests

from .alerts import Alerter, format_opportunity
from .config import STATE_DIR, Settings
from .matching import TeamIndex
from .polymarket import PolyGame, PolymarketClient
from .scanner import ScanResult, scan
from .slips import SlipsAuthError, SlipsClient, SlipsMarket, parse_markets


def load_poly_games(settings: Settings) -> list[PolyGame]:
    poly = PolymarketClient()

    def one_league(league: str) -> tuple[str, list[PolyGame], list]:
        try:
            games = poly.games(league, settings.lookahead_days)
            # Team lists are only needed to resolve names, so skip leagues with no games.
            return league, games, poly.teams(league) if games else []
        except requests.RequestException as e:
            print(f"[polymarket] {league}: {e}", file=sys.stderr)
            return league, [], []

    games: list[PolyGame] = []
    teams = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for league, league_games, league_teams in pool.map(one_league, settings.leagues):
            games.extend(league_games)
            teams[league] = league_teams
    index = TeamIndex(teams)
    for g in games:
        for o in g.outcomes:
            o.team = index.resolve(g.league, o.name)
    return games


def print_result(result: ScanResult, show_all: bool) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\n[{stamp}] compared {len(result.all_compared) // 2} games · {len(result.unmatched)} unmatched · "
          f"{result.skipped_empty} empty pools")
    if show_all and result.all_compared:
        print(f"\n{'Slips pick':<26}{'Slips':>6}{'Poly':>6}{'Edge':>8}{'Pool':>7}{'EV*':>6}  Game / note")
        for o in result.all_compared:
            pool = f"${o.slips.pool_total:,.0f}" if o.slips.pool_total is not None else "?"
            note = f"  ← {o.skip_reason}" if o.skip_reason and o.edge > 0 else ""
            print(f"{o.pick[:25]:<26}{o.slips_prob:>6.0%}{o.poly_prob:>6.0%}{o.edge * 100:>+7.1f}p{pool:>7}{o.ev:>+6.0%}  {o.poly.title} · {o.slips.description}{note}")
        print(f"  *EV assumes a ${result.all_compared[0].stake:g} bet plus Slips' fee")
    if show_all and result.unmatched:
        print("\nUnmatched Slips pools (no Polymarket game found):")
        for m in result.unmatched[:25]:
            pool = f"${m.pool_total:,.0f}" if m.pool_total is not None else "?"
            print(f"  - {m.title} · {m.description} [{m.sport}, {pool} pool] " + " / ".join(f"{s.name} {s.percentage:g}%" for s in m.sides))
    if result.opportunities:
        print(f"\n{len(result.opportunities)} opportunit{'y' if len(result.opportunities) == 1 else 'ies'} above threshold:\n")
        for o in result.opportunities:
            print(format_opportunity(o) + "\n")
    else:
        print("No opportunities above threshold.")


def run_scan(settings: Settings, slips_markets: list[SlipsMarket], show_all: bool, alert: bool) -> None:
    games = load_poly_games(settings)
    result = scan(slips_markets, games, settings)
    print_result(result, show_all)
    if alert:
        alerter = Alerter(settings)
        fresh = alerter.new_only(result.opportunities)
        alerter.send(fresh)
        if fresh:
            print(f"Sent {len(fresh)} new alert(s).")


def cmd_scan(args, settings: Settings) -> int:
    try:
        client = SlipsClient(settings.slips_access_token, settings.slips_refresh_token)
    except SlipsAuthError as e:
        print(e, file=sys.stderr)
        return 2
    dump_dir = STATE_DIR / "dumps" if args.dump else None
    while True:
        try:
            markets, status = client.fetch_feeds(dump_dir)
            if args.verbose:
                for feed, line in status.items():
                    print(f"  slips {feed}: {line}")
            run_scan(settings, markets, args.all, alert=True)
        except SlipsAuthError as e:
            print(e, file=sys.stderr)
            return 2
        except Exception as e:  # keep the loop alive through transient failures
            if not args.loop:
                raise
            print(f"[scan] error: {e}", file=sys.stderr)
        if not args.loop:
            return 0
        time.sleep(args.loop)


def cmd_demo(args, settings: Settings) -> int:
    """Run the full pipeline against a saved Slips response and live Polymarket prices."""
    payload = json.loads(files("iloveedging").joinpath("data/slips_sample.json").read_text())
    markets = parse_markets(payload)
    print(f"Loaded {len(markets)} synthetic pools (fake Slips data, live Polymarket prices)")
    run_scan(settings, markets, show_all=True, alert=False)
    return 0


def cmd_poly(args, settings: Settings) -> int:
    games = load_poly_games(settings)
    games.sort(key=lambda g: (g.start is None, g.start))
    for g in games:
        when = g.start.strftime("%m-%d %H:%M") if g.start else "?"
        sides = "  ".join(f"{o.name} {o.prob:.0%}" for o in g.outcomes)
        print(f"[{g.league}] {when}  {g.title:<45} {sides}  vol ${g.volume:,.0f}")
    print(f"\n{len(games)} upcoming games with a moneyline")
    return 0


def cmd_backend(args, settings: Settings) -> int:
    from .server import serve

    serve(settings, args.port, args.interval)
    return 0


def cmd_tui(args, settings: Settings) -> int:
    try:
        from .terminal import run_terminal
    except ModuleNotFoundError as error:
        if error.name not in {"textual", "rich"}:
            raise
        print("Install the app first: python3 -m pip install -e .", file=sys.stderr)
        return 2
    return run_terminal(args.port, args.interval, args.connect_only)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iloveedging", description="Terminal-only Slips × Polymarket intelligence. Run without a subcommand to launch the dashboard.")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="scan Slips vs Polymarket (needs Slips tokens)")
    p_scan.add_argument("--loop", type=int, metavar="SECONDS", help="rescan forever every N seconds")
    p_scan.add_argument("--threshold", type=float, help="min edge in probability points, e.g. 0.08")
    p_scan.add_argument("--min-ev", type=float, help="min expected return per $1, e.g. 0.05")
    p_scan.add_argument("--bet", type=float, help="bet size in dollars used for the payout math (default 10)")
    p_scan.add_argument("--all", action="store_true", help="also print every matched game and unmatched pools")
    p_scan.add_argument("--dump", action="store_true", help="save raw Slips responses to .state/dumps/")
    p_scan.add_argument("-v", "--verbose", action="store_true")

    p_demo = sub.add_parser("demo", help="dry run with sample Slips data")
    p_demo.add_argument("--threshold", type=float)
    p_demo.add_argument("--min-ev", type=float)

    sub.add_parser("poly", help="list upcoming Polymarket games and prices")

    p_tui = sub.add_parser("tui", help="launch the interactive terminal dashboard")
    p_backend = sub.add_parser("backend", help="internal local scanner (normally started automatically)")
    for target in (parser, p_tui, p_backend):
        target.add_argument("--port", type=positive_int, default=8787)
        target.add_argument("--interval", type=positive_int, default=120, metavar="SECONDS")
    for target in (parser, p_tui):
        target.add_argument("--connect-only", action="store_true", help="use an existing scanner; never start one")

    args = parser.parse_args(argv)
    settings = Settings.load()
    if getattr(args, "threshold", None) is not None:
        settings.edge_threshold = args.threshold
    if getattr(args, "min_ev", None) is not None:
        settings.min_ev = args.min_ev
    if getattr(args, "bet", None) is not None:
        settings.bet_size = args.bet
    return {"scan": cmd_scan, "demo": cmd_demo, "poly": cmd_poly, "backend": cmd_backend, "tui": cmd_tui}[args.command or "tui"](args, settings)


if __name__ == "__main__":
    sys.exit(main())
