"""Deliver alerts for new opportunities, without repeating ourselves every scan."""

from __future__ import annotations

import json
import subprocess
import sys

import requests

from .config import STATE_DIR, Settings
from .scanner import Opportunity

SEEN_FILE = STATE_DIR / "alerted.json"
# Re-alert on the same pool side only if the edge grew by at least this much.
REALERT_EDGE_STEP = 0.05


def format_opportunity(o: Opportunity) -> str:
    when = o.slips.start.astimezone().strftime("%a %b %d %I:%M %p") if o.slips.start else "time n/a"
    pool = f"${o.slips.pool_total:,.0f} pool" if o.slips.pool_total is not None else "pool size n/a"
    lines = [
        f"BACK {o.pick} on Slips — {o.slips.title or o.poly.title}, {o.slips.description} ({when})",
        f"  Slips: {o.slips_prob:.0%}  |  Polymarket: {o.poly_prob:.0%}  |  edge +{o.edge * 100:.1f} pts  |  {pool}",
        f"  Bet ${o.stake:g} (+${o.fee:.2f} fee) → get back ${o.win_amount:.2f} if it wins  →  EV {o.ev:+.0%}",
    ]
    if o.opposing_money is not None:
        lines.append(f"  Most you can win here: ${o.opposing_money:,.0f} (money on the other side)")
    if o.already_bet:
        lines.append("  (You already have a bet on this side.)")
    lines += [
        f"  Slips: {o.slips.url or f'pool #{o.slips.id} (open in app)'}",
        f"  Polymarket: {o.poly.url} (vol ${o.market.volume:,.0f})",
    ]
    return "\n".join(lines)


class Alerter:
    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            self.seen: dict[str, float] = json.loads(SEEN_FILE.read_text())
        except (OSError, ValueError):
            self.seen = {}

    def new_only(self, opps: list[Opportunity]) -> list[Opportunity]:
        fresh = []
        for o in opps:
            last = self.seen.get(o.key)
            if last is None or o.edge >= last + REALERT_EDGE_STEP:
                fresh.append(o)
        return fresh

    def send(self, opps: list[Opportunity]) -> None:
        for o in opps:
            text = format_opportunity(o)
            self._discord(text)
            self._telegram(text)
            self._macos(o)
            self.seen[o.key] = o.edge
        if opps:
            SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            SEEN_FILE.write_text(json.dumps(self.seen))

    def _discord(self, text: str) -> None:
        if not self.settings.discord_webhook_url:
            return
        try:
            requests.post(self.settings.discord_webhook_url, json={"content": f"```{text}```"}, timeout=10).raise_for_status()
        except requests.RequestException as e:
            print(f"[alerts] Discord failed: {e}", file=sys.stderr)

    def _telegram(self, text: str) -> None:
        s = self.settings
        if not (s.telegram_bot_token and s.telegram_chat_id):
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                json={"chat_id": s.telegram_chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            ).raise_for_status()
        except requests.RequestException as e:
            print(f"[alerts] Telegram failed: {e}", file=sys.stderr)

    def _macos(self, o: Opportunity) -> None:
        if not self.settings.macos_notifications or sys.platform != "darwin":
            return
        title = f"Slips edge +{o.edge * 100:.0f} pts: {o.pick}"
        body = f"Slips {o.slips_prob:.0%} vs Poly {o.poly_prob:.0%} · EV {o.ev:+.0%}"
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
