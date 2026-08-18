#!/usr/bin/env python3
"""
Arbitage 2.0 backtest for NSE stock futures.

Rules implemented:
- Scan all NSE stock futures (STF) each trading day.
- Current-month future close must be below spot close.
- Next-month future close must be below current-month future close.
- Spot-current future basis must pass the minimum basis filter.
- Current-next future spread must be less than or equal to the spot-current
  future basis.
- One-lot spread value and leg liquidity must pass minimum filters.
- Enter one lot: buy current-month future, sell next-month future.
- Hold until the current-month expiry or until current future close is at/above
  spot close, whichever comes first.

The script uses NSE daily bhavcopy ZIP archives for cash and F&O data.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
CM_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36"


@dataclass
class FutQuote:
    symbol: str
    expiry: date
    close: float
    lot: int
    volume: int
    oi: int


@dataclass
class Position:
    symbol: str
    entry_date: date
    expiry: date
    next_expiry: date
    lot: int
    entry_spot: float
    entry_curr: float
    entry_next: float
    entry_basis: float
    entry_spread: float
    rank_on_day: int
    last_curr: float
    last_next: float
    last_mtm: float = 0.0


def parse_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def to_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def download(url: str, path: Path, retries: int = 3) -> bool:
    if path.exists() and path.stat().st_size > 0:
        return True

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/zip,text/csv,*/*",
            "Referer": "https://www.nseindia.com/",
        },
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read()
            if not content.startswith(b"PK"):
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 404, 503}:
                return False
        except Exception:
            if attempt == retries:
                return False
            time.sleep(1.5 * attempt)
    return False


def read_zip_csv(path: Path):
    with zipfile.ZipFile(path) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(".csv")]
        if not names:
            return
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            yield from csv.DictReader(text)


def load_day(day: date, raw_dir: Path):
    ymd = day.strftime("%Y%m%d")
    fo_path = raw_dir / f"fo_{ymd}.zip"
    cm_path = raw_dir / f"cm_{ymd}.zip"
    ok_fo = download(FO_URL.format(yyyymmdd=ymd), fo_path)
    ok_cm = download(CM_URL.format(yyyymmdd=ymd), cm_path)
    if not (ok_fo and ok_cm):
        return None

    spots = {}
    for row in read_zip_csv(cm_path):
        if row.get("FinInstrmTp") == "STK" and row.get("SctySrs") == "EQ":
            close = to_float(row.get("ClsPric", ""))
            if close > 0:
                spots[row["TckrSymb"]] = close

    futures_by_symbol = defaultdict(list)
    for row in read_zip_csv(fo_path):
        if row.get("FinInstrmTp") != "STF":
            continue
        close = to_float(row.get("ClsPric", ""))
        lot = to_int(row.get("NewBrdLotQty", ""))
        volume = to_int(row.get("TtlTradgVol", ""))
        if close <= 0 or lot <= 0:
            continue
        quote = FutQuote(
            symbol=row["TckrSymb"],
            expiry=parse_date(row["XpryDt"]),
            close=close,
            lot=lot,
            volume=volume,
            oi=to_int(row.get("OpnIntrst", "")),
        )
        futures_by_symbol[quote.symbol].append(quote)

    for quotes in futures_by_symbol.values():
        quotes.sort(key=lambda q: q.expiry)
    return spots, futures_by_symbol


def equity_stats(equity_curve, initial_capital: float):
    peak = initial_capital
    max_dd = 0.0
    max_dd_pct = 0.0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0.0
    return max_dd, max_dd_pct


def pct(num: float, den: float) -> float:
    return num / den * 100 if den else 0.0


@dataclass
class StrategyRules:
    name: str = "Arbitage 2.0"
    min_basis: float = 10.0
    min_basis_pct: float = 0.0
    max_spread_to_basis: float = 1.0
    min_spread_value: float = 2000.0
    min_current_volume: int = 100
    min_next_volume: int = 100
    min_days_to_expiry: int = 0
    max_days_to_expiry: int = 999


def passes_entry_rules(spot: float, curr: FutQuote, nxt: FutQuote, rules: StrategyRules, scan_date: date) -> bool:
    basis = spot - curr.close
    spread = curr.close - nxt.close
    spread_value = spread * curr.lot
    basis_pct = basis / spot * 100 if spot else 0.0
    days_to_expiry = (curr.expiry - scan_date).days
    return (
        curr.close < spot
        and nxt.close < curr.close
        and basis >= rules.min_basis
        and basis_pct >= rules.min_basis_pct
        and spread <= basis * rules.max_spread_to_basis
        and spread_value >= rules.min_spread_value
        and curr.volume >= rules.min_current_volume
        and nxt.volume >= rules.min_next_volume
        and rules.min_days_to_expiry <= days_to_expiry <= rules.max_days_to_expiry
    )


def run_backtest(
    start: date,
    end: date,
    raw_dir: Path,
    out_dir: Path,
    initial_capital: float,
    rules: StrategyRules | None = None,
):
    rules = rules or StrategyRules()
    open_positions: dict[str, Position] = {}
    closed_trades = []
    daily_rows = []
    skipped_days = []
    signal_rows = []
    equity = initial_capital
    realized_pnl = 0.0
    trading_days = 0

    for day in daterange(start, end):
        if day.weekday() >= 5:
            continue
        loaded = load_day(day, raw_dir)
        if loaded is None:
            skipped_days.append(day.isoformat())
            continue
        trading_days += 1
        spots, futures_by_symbol = loaded

        day_realized = 0.0
        exits = []
        for symbol, pos in list(open_positions.items()):
            quotes = {q.expiry: q for q in futures_by_symbol.get(symbol, [])}
            curr = quotes.get(pos.expiry)
            nxt = quotes.get(pos.next_expiry)
            spot = spots.get(symbol)
            if curr is None or nxt is None or spot is None:
                continue
            pos.last_curr = curr.close
            pos.last_next = nxt.close
            mtm = (curr.close - pos.entry_curr) * pos.lot + (pos.entry_next - nxt.close) * pos.lot
            day_mtm_delta = mtm - pos.last_mtm
            equity += day_mtm_delta
            pos.last_mtm = mtm

            exit_reason = None
            if curr.close >= spot:
                exit_reason = "spot_matched_or_crossed_current_future"
            if day >= pos.expiry:
                exit_reason = "current_month_expiry" if exit_reason is None else exit_reason + "+expiry"
            if exit_reason:
                pnl = mtm
                day_realized += pnl
                realized_pnl += pnl
                holding_days = (day - pos.entry_date).days
                closed_trades.append(
                    {
                        "symbol": symbol,
                        "entry_date": pos.entry_date.isoformat(),
                        "exit_date": day.isoformat(),
                        "holding_days": holding_days,
                        "expiry": pos.expiry.isoformat(),
                        "next_expiry": pos.next_expiry.isoformat(),
                        "lot": pos.lot,
                        "entry_spot": round(pos.entry_spot, 4),
                        "entry_curr_future": round(pos.entry_curr, 4),
                        "entry_next_future": round(pos.entry_next, 4),
                        "exit_spot": round(spot, 4),
                        "exit_curr_future": round(curr.close, 4),
                        "exit_next_future": round(nxt.close, 4),
                        "entry_basis_spot_minus_curr": round(pos.entry_basis, 4),
                        "entry_spread_curr_minus_next": round(pos.entry_spread, 4),
                        "entry_spread_value": round(pos.entry_spread * pos.lot, 2),
                        "pnl": round(pnl, 2),
                        "return_on_capital_pct": round(pnl / initial_capital * 100, 4),
                        "exit_reason": exit_reason,
                        "rank_on_day": pos.rank_on_day,
                    }
                )
                exits.append(symbol)
        for symbol in exits:
            del open_positions[symbol]

        candidates = []
        for symbol, quotes in futures_by_symbol.items():
            if symbol in open_positions or symbol not in spots:
                continue
            live_quotes = [q for q in quotes if q.expiry >= day and q.volume > 0]
            if len(live_quotes) < 2:
                continue
            curr, nxt = live_quotes[0], live_quotes[1]
            spot = spots[symbol]
            if passes_entry_rules(spot, curr, nxt, rules, day):
                spread = curr.close - nxt.close
                basis = spot - curr.close
                basis_spread_ratio = basis / spread if spread else 999.0
                candidates.append((basis_spread_ratio, basis, -spread, symbol, curr, nxt, spot))

        candidates.sort(reverse=True)
        for rank, (basis_spread_ratio, basis, neg_spread, symbol, curr, nxt, spot) in enumerate(candidates, 1):
            spread = -neg_spread
            pos = Position(
                symbol=symbol,
                entry_date=day,
                expiry=curr.expiry,
                next_expiry=nxt.expiry,
                lot=curr.lot,
                entry_spot=spot,
                entry_curr=curr.close,
                entry_next=nxt.close,
                entry_basis=basis,
                entry_spread=spread,
                rank_on_day=rank,
                last_curr=curr.close,
                last_next=nxt.close,
            )
            open_positions[symbol] = pos
            signal_rows.append(
                {
                    "date": day.isoformat(),
                    "rank": rank,
                    "symbol": symbol,
                    "spot": round(spot, 4),
                    "curr_expiry": curr.expiry.isoformat(),
                    "next_expiry": nxt.expiry.isoformat(),
                    "curr_future": round(curr.close, 4),
                    "next_future": round(nxt.close, 4),
                    "spot_minus_curr": round(basis, 4),
                    "curr_minus_next": round(spread, 4),
                    "basis_to_spread_ratio": round(basis_spread_ratio, 4),
                    "basis_pct_of_spot": round(basis / spot * 100 if spot else 0, 4),
                    "days_to_expiry": (curr.expiry - day).days,
                    "spread_value_one_lot": round(spread * curr.lot, 2),
                    "lot": curr.lot,
                    "curr_volume": curr.volume,
                    "next_volume": nxt.volume,
                    "min_basis_rule": rules.min_basis,
                    "min_basis_pct_rule": rules.min_basis_pct,
                    "max_spread_to_basis_rule": rules.max_spread_to_basis,
                    "min_spread_value_rule": rules.min_spread_value,
                    "min_current_volume_rule": rules.min_current_volume,
                    "min_next_volume_rule": rules.min_next_volume,
                    "min_days_to_expiry_rule": rules.min_days_to_expiry,
                    "max_days_to_expiry_rule": rules.max_days_to_expiry,
                }
            )

        open_mtm = sum(pos.last_mtm for pos in open_positions.values())
        daily_rows.append(
            {
                "date": day.isoformat(),
                "equity": round(equity, 2),
                "open_positions": len(open_positions),
                "new_signals": len(candidates),
                "day_realized_pnl": round(day_realized, 2),
                "realized_pnl_cumulative": round(realized_pnl, 2),
                "open_mtm": round(open_mtm, 2),
            }
        )

    # Force close open positions on the final available day if marked prices exist.
    if open_positions and daily_rows:
        final_day = parse_date(daily_rows[-1]["date"])
        loaded = load_day(final_day, raw_dir)
        if loaded:
            spots, futures_by_symbol = loaded
            for symbol, pos in list(open_positions.items()):
                quotes = {q.expiry: q for q in futures_by_symbol.get(symbol, [])}
                curr = quotes.get(pos.expiry)
                nxt = quotes.get(pos.next_expiry)
                spot = spots.get(symbol)
                if curr and nxt and spot:
                    pnl = (curr.close - pos.entry_curr) * pos.lot + (pos.entry_next - nxt.close) * pos.lot
                    closed_trades.append(
                        {
                            "symbol": symbol,
                            "entry_date": pos.entry_date.isoformat(),
                            "exit_date": final_day.isoformat(),
                            "holding_days": (final_day - pos.entry_date).days,
                            "expiry": pos.expiry.isoformat(),
                            "next_expiry": pos.next_expiry.isoformat(),
                            "lot": pos.lot,
                            "entry_spot": round(pos.entry_spot, 4),
                            "entry_curr_future": round(pos.entry_curr, 4),
                            "entry_next_future": round(pos.entry_next, 4),
                            "exit_spot": round(spot, 4),
                            "exit_curr_future": round(curr.close, 4),
                            "exit_next_future": round(nxt.close, 4),
                            "entry_basis_spot_minus_curr": round(pos.entry_basis, 4),
                            "entry_spread_curr_minus_next": round(pos.entry_spread, 4),
                            "entry_spread_value": round(pos.entry_spread * pos.lot, 2),
                            "pnl": round(pnl, 2),
                            "return_on_capital_pct": round(pnl / initial_capital * 100, 4),
                            "exit_reason": "forced_end_of_backtest",
                            "rank_on_day": pos.rank_on_day,
                        }
                    )
            open_positions.clear()

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "trades.csv", closed_trades)
    write_csv(out_dir / "daily_equity.csv", daily_rows)
    write_csv(out_dir / "signals.csv", signal_rows)

    gross_profit = sum(float(t["pnl"]) for t in closed_trades if float(t["pnl"]) > 0)
    gross_loss = sum(float(t["pnl"]) for t in closed_trades if float(t["pnl"]) < 0)
    net_pnl = gross_profit + gross_loss
    wins = [t for t in closed_trades if float(t["pnl"]) > 0]
    losses = [t for t in closed_trades if float(t["pnl"]) < 0]
    max_dd, max_dd_pct = equity_stats([(parse_date(r["date"]), float(r["equity"])) for r in daily_rows], initial_capital)
    summary = {
        "strategy_name": rules.name,
        "entry_rule": "spot > current_future > next_future",
        "min_basis_spot_minus_current": rules.min_basis,
        "min_basis_pct_of_spot": rules.min_basis_pct,
        "max_spread_to_basis": rules.max_spread_to_basis,
        "min_spread_value_one_lot": rules.min_spread_value,
        "min_current_future_volume": rules.min_current_volume,
        "min_next_future_volume": rules.min_next_volume,
        "min_days_to_expiry": rules.min_days_to_expiry,
        "max_days_to_expiry": rules.max_days_to_expiry,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "initial_capital": initial_capital,
        "trading_days_loaded": trading_days,
        "skipped_weekdays_no_data": len(skipped_days),
        "signals": len(signal_rows),
        "closed_trades": len(closed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(pct(len(wins), len(closed_trades)), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net_pnl, 2),
        "return_pct_on_5l_capital": round(net_pnl / initial_capital * 100, 2),
        "profit_factor": round(gross_profit / abs(gross_loss), 3) if gross_loss else None,
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_trade_pnl": round(net_pnl / len(closed_trades), 2) if closed_trades else 0,
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_loss / len(losses), 2) if losses else 0,
        "max_win": round(max((float(t["pnl"]) for t in closed_trades), default=0), 2),
        "max_loss": round(min((float(t["pnl"]) for t in closed_trades), default=0), 2),
        "avg_holding_days": round(sum(int(t["holding_days"]) for t in closed_trades) / len(closed_trades), 2) if closed_trades else 0,
        "max_open_positions": max((int(r["open_positions"]) for r in daily_rows), default=0),
        "last_equity": round(initial_capital + net_pnl, 2),
        "skipped_days_sample": skipped_days[:20],
    }
    (out_dir / "summary.txt").write_text("\n".join(f"{k}: {v}" for k, v in summary.items()) + "\n")
    return summary


def write_csv(path: Path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-05-15")
    parser.add_argument("--end", default="2026-05-15")
    parser.add_argument("--capital", type=float, default=500000)
    parser.add_argument("--raw-dir", default="data/raw")
    parser.add_argument("--out-dir", default="results")
    parser.add_argument("--strategy-name", default="Arbitage 2.0")
    parser.add_argument("--min-basis", type=float, default=10.0)
    parser.add_argument("--min-basis-pct", type=float, default=0.0)
    parser.add_argument("--max-spread-to-basis", type=float, default=1.0)
    parser.add_argument("--min-spread-value", type=float, default=2000.0)
    parser.add_argument("--min-current-volume", type=int, default=100)
    parser.add_argument("--min-next-volume", type=int, default=100)
    parser.add_argument("--min-days-to-expiry", type=int, default=0)
    parser.add_argument("--max-days-to-expiry", type=int, default=999)
    args = parser.parse_args()

    summary = run_backtest(
        start=parse_date(args.start),
        end=parse_date(args.end),
        raw_dir=Path(args.raw_dir),
        out_dir=Path(args.out_dir),
        initial_capital=args.capital,
        rules=StrategyRules(
            name=args.strategy_name,
            min_basis=args.min_basis,
            min_basis_pct=args.min_basis_pct,
            max_spread_to_basis=args.max_spread_to_basis,
            min_spread_value=args.min_spread_value,
            min_current_volume=args.min_current_volume,
            min_next_volume=args.min_next_volume,
            min_days_to_expiry=args.min_days_to_expiry,
            max_days_to_expiry=args.max_days_to_expiry,
        ),
    )
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
