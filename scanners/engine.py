#!/usr/bin/env python3
"""Daily paper-trading engine for Arbitrage 2.0 / 2.1.

Usage:
    python engine.py --date 2026-07-09                 # v2.1 (default), write
    python engine.py --date 2026-07-09 --preset v2.0   # v2.0 rules
    python engine.py --date 2026-07-09 --dry-run       # preview, no writes

Flow per run:
  1. Exit pass  — for every OPEN position across all ledgers, fetch the
     scan day's bhavcopy and apply the exit rule (current future >= spot,
     or current-month expiry reached). Close qualifiers, realize P&L.
  2. Kill-switch — if the day's realized losses breach max_loss_per_day,
     stop opening new positions for the rest of that day.
  3. Entry pass — scan the day for candidates (excluding symbols already
     open), respect max_concurrent, append OPEN rows via the broker.

Outputs a results/live_YYYY-MM-DD/ run dir with signals.csv,
daily_equity.csv, and summary.txt, plus a human-readable stdout report.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import backtest_future_arbitaage as bt
import broker as broker_mod
import config as config_mod
import ledger
import presets
import scanner


def _to_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _to_date_or_none(value: str) -> date | None:
    if not value:
        return None
    try:
        return _to_date(value[:10])
    except ValueError:
        return None


# --- Exit pass -----------------------------------------------------------


def exit_pass(
    scan_date: date,
    cfg: config_mod.Config,
) -> tuple[list[dict], float]:
    """Close every OPEN position whose exit rule has fired by scan_date.

    Walks all ledger files (an entry may have been opened on an earlier
    day). Returns (closed_rows, day_realized_pnl).
    """
    loaded = bt.load_day(scan_date, Path(cfg.raw_dir))
    if loaded is None:
        return [], 0.0
    spots, futures_by_symbol = loaded

    closed_rows = []
    day_realized = 0.0

    for path in ledger.all_ledgers(cfg.trades_dir):
        changed = False
        for row in ledger.load_open(path):
            symbol = row["symbol"]
            curr_exp = _to_date_or_none(row["current_expiry"])
            next_exp = _to_date_or_none(row["next_expiry"])
            entry_curr = _as_float(row["entry_current_future"])
            entry_next = _as_float(row["entry_next_future"])
            lot = _as_int(row["lot"])
            entry_date = _to_date_or_none(row["paper_entry_date"]) or scan_date

            spot = spots.get(symbol)
            quotes = {q.expiry: q for q in futures_by_symbol.get(symbol, [])}
            curr_q = quotes.get(curr_exp) if curr_exp else None
            nxt_q = quotes.get(next_exp) if next_exp else None
            if spot is None or curr_q is None or nxt_q is None:
                # No data for this symbol on scan_date — leave it open.
                continue

            # Exit rules (mirror backtest_future_arbitaage.run_backtest):
            #   current future >= spot  -> convergence
            #   scan_date >= expiry     -> current-month expiry
            reason = None
            if curr_q.close >= spot:
                reason = ledger.EXIT_REASON_CONVERGENCE
            elif curr_exp and scan_date >= curr_exp:
                reason = ledger.EXIT_REASON_EXPIRY
            if reason is None:
                continue

            pnl = (curr_q.close - entry_curr) * lot + (entry_next - nxt_q.close) * lot
            day_realized += pnl

            ledger.mark_closed(
                path,
                symbol,
                exit_date=scan_date,
                exit_reason=reason,
                exit_spot=spot,
                exit_current_future=curr_q.close,
                exit_next_future=nxt_q.close,
                paper_pnl=pnl,
            )
            changed = True
            closed_rows.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_date.isoformat(),
                    "exit_date": scan_date.isoformat(),
                    "exit_reason": reason,
                    "exit_spot": round(spot, 4),
                    "exit_curr": round(curr_q.close, 4),
                    "exit_next": round(nxt_q.close, 4),
                    "pnl": round(pnl, 2),
                }
            )
        # mark_closed rewrites the file; nothing else to flush here.
        _ = changed

    return closed_rows, day_realized


# --- Entry pass ----------------------------------------------------------


def entry_pass(
    scan_date: date,
    cfg: config_mod.Config,
    broker: broker_mod.Broker,
    *,
    dry_run: bool,
) -> list[scanner.Candidate]:
    """Scan for new candidates and open up to max_concurrent.

    Skips symbols already open anywhere in the ledgers. Respects the
    max_loss_per_day kill-switch (caller passes day_realized separately
    via run()). Returns the full ranked candidate list (for signals.csv),
    not just the ones entered.
    """
    already_open: set[str] = set()
    for path in ledger.all_ledgers(cfg.trades_dir):
        already_open |= ledger.open_symbols(path)

    candidates = scanner.scan_day(
        scan_date, cfg.rules, cfg.raw_dir, exclude=already_open
    )
    return candidates


# --- Run output ----------------------------------------------------------


def _write_signals(out_dir: Path, candidates: list[scanner.Candidate]) -> None:
    fields = [
        "rank", "symbol", "scan_date", "spot", "curr_expiry", "next_expiry",
        "curr_future", "next_future", "basis", "spread", "basis_spread_ratio",
        "basis_pct", "spread_value", "lot", "curr_volume", "next_volume",
        "days_to_expiry",
    ]
    with (out_dir / "signals.csv").open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for c in candidates:
            d = asdict(c)
            d["scan_date"] = c.scan_date.isoformat()
            d["curr_expiry"] = c.curr_expiry.isoformat()
            d["next_expiry"] = c.next_expiry.isoformat()
            writer.writerow({k: d.get(k, "") for k in fields})


def _write_equity(
    out_dir: Path, *, capital: float, open_count: int, day_pnl: float,
    realized_cum: float, open_mtm: float, new_signals: int,
) -> None:
    fields = [
        "date", "equity", "open_positions", "new_signals",
        "day_realized_pnl", "realized_pnl_cumulative", "open_mtm",
    ]
    with (out_dir / "daily_equity.csv").open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "date": _today_str(),
                "equity": round(capital + realized_cum + open_mtm, 2),
                "open_positions": open_count,
                "new_signals": new_signals,
                "day_realized_pnl": round(day_pnl, 2),
                "realized_pnl_cumulative": round(realized_cum, 2),
                "open_mtm": round(open_mtm, 2),
            }
        )


def _write_summary(out_dir: Path, lines: list[str]) -> None:
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --- Open-position MTM (mark to market against scan_date close) ----------


def open_mtm(scan_date: date, cfg: config_mod.Config) -> tuple[int, float]:
    """Return (open_count, total_unrealized_mtm) across all ledgers."""
    loaded = bt.load_day(scan_date, Path(cfg.raw_dir))
    if loaded is None:
        # No bhavcopy for scan_date — count opens without marking.
        count = sum(len(ledger.load_open(p)) for p in ledger.all_ledgers(cfg.trades_dir))
        return count, 0.0
    spots, futures_by_symbol = loaded
    mtm = 0.0
    count = 0
    for path in ledger.all_ledgers(cfg.trades_dir):
        for row in ledger.load_open(path):
            count += 1
            symbol = row["symbol"]
            curr_exp = _to_date_or_none(row["current_expiry"])
            next_exp = _to_date_or_none(row["next_expiry"])
            quotes = {q.expiry: q for q in futures_by_symbol.get(symbol, [])}
            curr_q = quotes.get(curr_exp) if curr_exp else None
            nxt_q = quotes.get(next_exp) if next_exp else None
            if not curr_q or not nxt_q:
                continue
            lot = _as_int(row["lot"])
            entry_curr = _as_float(row["entry_current_future"])
            entry_next = _as_float(row["entry_next_future"])
            mtm += (curr_q.close - entry_curr) * lot + (entry_next - nxt_q.close) * lot
    return count, mtm


# --- Main run ------------------------------------------------------------


def run(
    scan_date: date,
    cfg: config_mod.Config,
    *,
    preset_override: str | None = None,
    dry_run: bool = False,
    broker_mode: str = "paper",
) -> dict:
    if preset_override:
        cfg.preset = preset_override
    rules = cfg.rules

    broker = broker_mod.make_broker(broker_mode, cfg.trades_dir)

    # 1. Exit pass.
    closed_rows, day_realized = exit_pass(scan_date, cfg)

    # 2. Kill-switch: tripped if today's realized losses breach the limit.
    kill_switch = day_realized <= -cfg.max_loss_per_day

    # 3. Entry pass.
    candidates = entry_pass(scan_date, cfg, broker, dry_run=dry_run)

    # How many can we still open?
    open_count_before, _ = open_mtm(scan_date, cfg)
    slots = max(0, cfg.max_concurrent - open_count_before)
    if kill_switch:
        slots = 0

    entered: list[scanner.Candidate] = []
    if not dry_run:
        for cand in candidates:
            if len(entered) >= slots:
                break
            fill = broker.open_spread(cand, lots=cfg.lots_per_signal)
            if fill.ok:
                entered.append(cand)
    else:
        # In dry-run, "enter" the first `slots` virtually for reporting.
        entered = candidates[:slots]

    # Recompute open count + MTM after entries.
    open_count, mtm = open_mtm(scan_date, cfg)

    # Cumulative realized P&L across the whole paper portfolio.
    realized_cum = 0.0
    for path in ledger.all_ledgers(cfg.trades_dir):
        for row in ledger.load_all(path):
            if row.get("status") == "CLOSED":
                realized_cum += _as_float(row.get("paper_pnl"))

    # Run dir.
    out_dir = Path(cfg.results_dir) / f"live_{scan_date.isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_signals(out_dir, candidates)
    _write_equity(
        out_dir,
        capital=cfg.capital,
        open_count=open_count,
        day_pnl=day_realized,
        realized_cum=realized_cum,
        open_mtm=mtm,
        new_signals=len(candidates),
    )
    summary_lines = [
        f"strategy_name: {rules.name}",
        f"preset: {cfg.preset}",
        f"scan_date: {scan_date.isoformat()}",
        f"capital: {cfg.capital}",
        f"max_concurrent: {cfg.max_concurrent}",
        f"max_loss_per_day_kill_switch: {cfg.max_loss_per_day}",
        f"kill_switch_tripped: {kill_switch}",
        f"dry_run: {dry_run}",
        f"signals: {len(candidates)}",
        f"entered: {len(entered)}",
        f"closed_today: {len(closed_rows)}",
        f"day_realized_pnl: {round(day_realized, 2)}",
        f"realized_pnl_cumulative: {round(realized_cum, 2)}",
        f"open_positions: {open_count}",
        f"open_mtm: {round(mtm, 2)}",
        f"equity: {round(cfg.capital + realized_cum + mtm, 2)}",
        f"rules: min_basis={rules.min_basis} min_basis_pct={rules.min_basis_pct} "
        f"max_spread_to_basis={rules.max_spread_to_basis} "
        f"min_next_volume={rules.min_next_volume} "
        f"dte_window={rules.min_days_to_expiry}-{rules.max_days_to_expiry}",
    ]
    _write_summary(out_dir, summary_lines)

    return {
        "preset": cfg.preset,
        "scan_date": scan_date,
        "dry_run": dry_run,
        "candidates": candidates,
        "entered": entered,
        "closed_rows": closed_rows,
        "day_realized": day_realized,
        "realized_cum": realized_cum,
        "open_count": open_count,
        "mtm": mtm,
        "kill_switch": kill_switch,
        "capital": cfg.capital,
        "out_dir": out_dir,
    }


def _print_report(result: dict) -> None:
    d = result["scan_date"].isoformat()
    print(f"\n=== Arbitrage paper run | {d} | preset={result['preset']} "
          f"| dry_run={result['dry_run']} ===")
    if result["closed_rows"]:
        print(f"\n-- closed today ({len(result['closed_rows'])}) --")
        for r in result["closed_rows"]:
            print(f"  {r['symbol']:14} pnl={r['pnl']:>10}  ({r['exit_reason']})")
    print(f"\n-- candidates ({len(result['candidates'])}) --")
    for c in result["candidates"]:
        print(
            f"  #{c.rank:<2} {c.symbol:14} spot={c.spot:>10.2f} "
            f"curr={c.curr_future:>10.2f} next={c.next_future:>10.2f} "
            f"basis={c.basis:>7.2f} spread={c.spread:>7.2f} "
            f"ratio={c.basis_spread_ratio:>5.2f} sv={c.spread_value:>9.0f} "
            f"dte={c.days_to_expiry}"
        )
    if result["entered"]:
        print(f"\n-- entered ({len(result['entered'])}) --")
        for c in result["entered"]:
            print(f"  {c.symbol:14} BUY {c.curr_expiry.isoformat()} / "
                  f"SELL {c.next_expiry.isoformat()}  lot={c.lot}")
    print(
        f"\nclosed_today={len(result['closed_rows'])}  "
        f"day_realized={result['day_realized']:.2f}  "
        f"realized_cum={result['realized_cum']:.2f}\n"
        f"open={result['open_count']}  open_mtm={result['mtm']:.2f}  "
        f"equity={result['capital'] + result['realized_cum'] + result['mtm']:.2f} "
        f"(capital + realized + mtm)\n"
        f"kill_switch_tripped={result['kill_switch']}\n"
        f"run_dir: {result['out_dir']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage 2.0/2.1 paper engine")
    parser.add_argument(
        "--date", default=datetime.now().strftime("%Y-%m-%d"),
        help="scan date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--preset", default=None,
        help="rule preset: v2.0 or v2.1 (default: from config / v2.1)",
    )
    parser.add_argument("--dry-run", action="store_true", help="no ledger writes")
    parser.add_argument(
        "--broker", default="paper", choices=["paper", "kite"],
        help="broker mode (kite is stubbed in M1)",
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    args = parser.parse_args()

    cfg = config_mod.load(args.config)
    if args.broker == "kite":
        # Force a clear failure rather than a silent paper run if someone
        # asks for kite mode before M2 is implemented.
        broker_mod.KiteBroker(cfg.trades_dir)  # constructs fine; methods raise

    result = run(
        _to_date(args.date),
        cfg,
        preset_override=args.preset,
        dry_run=args.dry_run,
        broker_mode=args.broker,
    )
    _print_report(result)


if __name__ == "__main__":
    main()
