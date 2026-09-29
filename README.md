# iloveedging

A terminal-only workspace for comparing **Slips sports pools** with **Polymarket prices**.
Dark charcoal, lime/cyan odds, live scans, searchable markets, and modeled payout details.
No React, browser dashboard, Node.js, or frontend build. **It never places bets.**

## Quick start

Requires **Python 3.11+**, Git, and a terminal. An 80×24 window works; 140×44 gives you the
side-by-side market inspector. Live Slips data requires your own Slips session tokens.
Polymarket data is public and does not require a Polymarket account.

### macOS / Linux

```bash
git clone https://github.com/vy28/iloveedging.git
cd iloveedging
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Fill in your own Slips tokens in `.env` using the instructions below, then launch:

```bash
iloveedging
```

### Windows (PowerShell)

```powershell
git clone https://github.com/vy28/iloveedging.git
cd iloveedging
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
# Edit .env with your own Slips tokens, then:
.\.venv\Scripts\python.exe -m iloveedging
```

Using the virtual environment's Python directly avoids changing PowerShell execution policy.
`python -m iloveedging` and `iloveedging tui` are equivalent to `iloveedging`.
The command is installed locally by `pip install -e .`; do not install an unrelated PyPI package by name.

### Next time / updates

```bash
cd iloveedging
source .venv/bin/activate
git pull
python -m pip install -e .
iloveedging
```

You can skip `git pull` and installation when you only want to launch the existing version.
Your `.env` and `.state/` stay local and are excluded from Git. Each person cloning the project
must configure their own credentials; nothing from the original author's account is included.

## Controls

| Key | Action |
|---|---|
| `1` / `2` / `3` | Qualified opportunities / all compared markets / unmatched pools |
| `↑` / `↓`, `Enter` | Select a market; open its full inspector |
| `/`, `Enter`, `Esc` | Search; return to the table; clear search / close dialog |
| `F` | Set minimum Poly probability (0–100%, decimals accepted) |
| `S` | Sort by expected return, Poly probability, or edge |
| `R` | Request a fresh scan |
| `E` | Edit and save scanner rules and modeled stake |
| `Q` / `Ctrl+Q` | Quit (`Ctrl+Q` also works while typing) |

Mouse navigation works too. The probability filter defaults to **62%**, but you can choose any
minimum. **0%** shows all probabilities. It applies to Opportunities and All markets for the
current terminal session, not to scanner alerts. The percentage is a market estimate, not a guarantee.

The first scan can take a minute. On connection failures the last snapshot remains visible with
an offline/error status; old results are marked stale. A return **if you win** includes your stake;
net profit and maximum loss are shown separately.

## Getting your Slips tokens

Slips only returns the sports feeds to logged-in users, so the bot uses your own session.

1. Log in at slips.com in Chrome.
2. Press Cmd+Option+J to open the **Console**. If Chrome warns you about pasting, type `allow pasting` and press Enter.
3. Paste this and press Enter. It copies both tokens to your clipboard without showing them on screen:
   ```js
   (() => {
     const found = {};
     try {
       const d = JSON.parse(decodeURIComponent(atob(localStorage.getItem("ZGF0YQ==") || "")));
       for (const [k, v] of Object.entries(d)) if (k.includes("tokens")) found[k] = v;
     } catch (e) {}
     for (const c of document.cookie.split("; ")) {
       const i = c.indexOf("=");
       const k = decodeURIComponent(c.slice(0, i));
       if (k.includes("tokens")) found[k] = decodeURIComponent(c.slice(i + 1));
     }
     const get = (end) => Object.entries(found).find(([k]) => k.endsWith(end))?.[1];
     const access = get("tokens.access.token"), refresh = get("tokens.refresh.token");
     if (!access || !refresh) return "NOT FOUND. Keys seen: " + Object.keys(found).join(", ");
     copy(`SLIPS_ACCESS_TOKEN=${access}\nSLIPS_REFRESH_TOKEN=${refresh}`);
     return "Copied! Now paste into your .env file.";
   })()
   ```
4. Open `.env` in your editor, replace the two empty `SLIPS_..._TOKEN=` lines with what you copied, and save. Use your own account; never share these values.

These tokens give full access to your Slips account, so treat them like a password. Don't
share `.env` or commit it. When the access token expires the bot refreshes it and saves the
new pair to `.state/slips_tokens.json`, which is readable only by you. If refreshing fails, copy fresh tokens.


## Running and configuration

The app loads `.env` and `.state/` from the **working directory**, so run it from your clone.
To keep configuration somewhere else, set `ILOVEEDGING_HOME` to that directory before launching.
Environment variables override `.env`. Rules saved with `E` take precedence over the six rule
values in `.env`; they are stored in `.state/ui_settings.json`.

```bash
iloveedging --interval 60              # rescan every minute (new scanner only)
iloveedging --port 8788                # use a different local port
iloveedging --connect-only             # use an already running scanner
iloveedging scan --all -v              # print one scan without the interactive UI
iloveedging scan --loop 120            # repeat scans with optional alerts
iloveedging scan --threshold 0.12 --min-ev 0.10 --bet 10
iloveedging poly                       # public Polymarket data; no Slips login
iloveedging demo                       # synthetic Slips fixture + live Poly data
```

`demo` is a console pipeline example, not live Slips data. Its historical fixture dates may no
longer match current Polymarket events. The offline test suite uses fully controlled fixtures.

The terminal automatically starts a **loopback-only scanner service** on `127.0.0.1:8787` and
stops its own child process on exit. There is **no web UI**: browser paths return a JSON 404.
An existing scanner on that port is reused and is not stopped on exit. Advanced users can run
`iloveedging backend` separately. Use a separate port when using a different configuration directory.
Do not expose this internal service to a network or reverse proxy.

### Optional alerts

Interactive mode starts its scanner with external notifications disabled. Console `scan` and
manually started `backend` commands honor these settings from your own `.env`:

- `MACOS_NOTIFICATIONS=1`: native notifications on macOS only.
- `DISCORD_WEBHOOK_URL`: Discord webhook for your channel.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: Telegram destination.

An already-running scanner keeps its own alert configuration. Each pool side alerts once, then
again only if the edge increases by another 5 points. All notification settings are off by default.

## How it works

Slips pools are pari-mutuel: winners split the opposing side's money. The scanner matches the
same teams, game date, bet type and line against Polymarket and models how your stake changes
that pool's payout. This is a **pricing comparison**, not guaranteed arbitrage.

| Rule | Default | Meaning |
|---|---|---|
| `EDGE_THRESHOLD` | 0.08 | Poly probability minus Slips probability must be ≥8 percentage points |
| `MIN_EV` | 0.05 | Expected return after fees and your pool impact must be ≥5% |
| `BET_SIZE` | 10 | Modeled stake in dollars |
| `MIN_OPPOSING_MONEY` | 25 | Minimum money on the other side of the pool |
| `MIN_POLY_VOLUME` | 1000 | Minimum volume of the matching Poly market |
| `MAX_POLY_SPREAD` | 0.05 | Maximum Poly bid/ask spread |

- Poly probability uses the bid/ask midpoint when available, otherwise the quoted outcome price.
- Slips return if you win = `stake × (pool total + stake) / (money on your side + stake)`.
- Expected return = `(Poly probability × return if win − stake − fee) / (stake + fee)`.
- Fees come from the pool's game settings, normally 5% with a $0.50 minimum.
- Moneylines, spreads and totals must match line-for-line. Props match only when the same player,
  stat and line exist. Empty pools and live games are skipped; the UI explains skipped comparisons.
- Matching requires both teams and start times within 12 hours, or 36 hours for midnight placeholders;
  the closest game is used for back-to-back fixtures. Soccer's three-way outcomes limit matching.

## Troubleshooting and safety

- **No Slips data / scan error:** fill in `.env` or refresh your own Slips tokens. Use
  `iloveedging scan --all -v` to inspect feed status. The private Slips API can change without notice.
- **No qualified picks:** lower your Poly filter with `F`, open All markets with `2`, or inspect
  your rules with `E`. No qualifying edge is a valid result, not a reason to force a bet.
- **Port occupied:** use `iloveedging --port 8788`; never kill unrelated processes.
- **Command not found:** activate your virtual environment or use its Python with `-m iloveedging`.
- **Raw diagnostics:** `iloveedging scan --all -v --dump` writes private responses to `.state/dumps/`.
  Never upload these, `.env`, refreshed tokens, or unredacted logs to GitHub or an issue.
- **Prices move:** other bets, injuries and news change both odds and returns. Verify the exact
  game, line and live payout before betting. Polymarket prices are not guaranteed probabilities.
- Only use your own authorized accounts and comply with platform terms and local laws.

## Development / tests

```bash
python -m pip install -e .
python -m unittest discover tests
python -m pip wheel . --no-deps -w dist
```

Tests run offline without credentials. GitHub Actions runs them on macOS, Linux and Windows
with Python 3.11–3.13. Python package data includes the terminal stylesheet and synthetic fixture.
