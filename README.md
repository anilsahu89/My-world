# NSE Alerts Automation — Setup Guide

Turns your alerts.html / paper.html into a page that reads from a JSON
file kept fresh by GitHub Actions, instead of depending on your machine
being on.

## Files

| File | Runs where | Runs when |
|---|---|---|
| `refresh_kite_token.py` | Your laptop/phone | Once, manually, every trading morning |
| `fetch_and_scan.py` | GitHub Actions | Every 5 min, 9:15–15:30 IST, automatically |
| `.github/workflows/fetch-alerts.yml` | GitHub Actions | The schedule definition |
| `symbols.txt` | — | Your NSE-200 watchlist (one symbol per line) — **you need to add this** |
| `data/alerts.json` | — | Generated output your page should `fetch()` |

## One-time setup

1. **Add these repo secrets** (repo → Settings → Secrets and variables → Actions):
   - `KITE_API_KEY` — from developer.kite.trade
   - `KITE_ACCESS_TOKEN` — leave any placeholder value for now; the refresh script updates it daily

2. **Create a GitHub Personal Access Token (PAT)** for the refresh script:
   - Settings → Developer settings → Personal access tokens → generate one with `repo` scope
   - Keep it locally, never commit it

3. **Add `symbols.txt`** to the repo root with your Nifty-200 tradingsymbols (e.g. `RELIANCE`, `TCS`, ...).

4. **Fill in your real BB Trap v2 rules** in `fetch_and_scan.py::compute_signals()` —
   right now it's a placeholder stub. Pull the 7-rule spec from your
   `wiki/strategies/` vault notes.

5. **Point `alerts.html` / `paper.html`** at the generated file, e.g.:
   ```js
   fetch("data/alerts.json")
     .then(r => r.json())
     .then(data => { /* render data.open_eq_low, data.open_eq_high, data.bb_trap_v2 */ });
   ```

## Every trading morning

Run, on any device with Python + internet (~60 seconds):

```bash
export KITE_API_KEY=...
export KITE_API_SECRET=...
export GITHUB_PAT=...
export GITHUB_REPO=anilsahu89/My-world
python refresh_kite_token.py
```

Log in when the browser opens, paste the redirect URL back in — that's it.
The rest of the day's scans run unattended on GitHub's servers.

## If you skip a morning

`fetch_and_scan.py` automatically falls back to yfinance if the Kite
token is missing or expired, so the page still updates — just with a
less reliable data source for that day. `alerts.json` includes a
`data_source` field so you can tell which one produced a given run.

## Notes

- Kite Connect quote() supports up to 500 instruments per call — your
  Nifty-200 list fits in one request per scan.
- The daily token requirement is a SEBI rule that applies to every
  Indian broker API (Kite, Upstox, Angel One, Fyers) — there's no
  legitimate way around the once-a-day login step.
- For Bitcoin/spot-gold feeds that need zero daily touch at all, that's
  a separate script (Binance/CoinGecko + a metals API) — happy to build
  that next.
