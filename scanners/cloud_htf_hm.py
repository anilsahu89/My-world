#!/usr/bin/env python3
"""Intraday HM — HTF (weekly+daily) bias, 1H entries. Cloud only."""
from __future__ import annotations
import argparse, csv, json, time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
import numpy as np, pandas as pd, yfinance as yf

BASE = Path(__file__).parent
ROOT = BASE.parent
DATA = ROOT / "data"
TRADES_FILE = BASE / "papertrades" / "htf_hm_trades.csv"
STATE_FILE = DATA / "cloud_intraday_hm_state.json"
OUT_DEFAULT = DATA / "scanners" / "htf-hm-latest.json"
CAPITAL, LOT, MAX_OPEN, MAX_NEW = 100_000.0, 10_000.0, 5, 3
RR, RSI_BOOK, COST = 1.5, 86, 0.003
EOD = dtime(15, 15)
IST = timezone(timedelta(hours=5, minutes=30))

def now(): return datetime.now(IST)
def rsi9(c):
    d=c.diff(); up=d.clip(lower=0).ewm(alpha=1/9,adjust=False).mean()
    dn=(-d.clip(upper=0)).ewm(alpha=1/9,adjust=False).mean()
    rs=up/dn.replace(0,np.nan); return (100-100/(1+rs)).fillna(50.0)
def wma(s,n):
    w=np.arange(1,n+1); return s.rolling(n).apply(lambda x: float((x*w).sum()/w.sum()), raw=True)
def attach_hm(df):
    if df is None or len(df)<40: return None
    r=rsi9(df["Close"]); out=df.assign(rsi=r,red=wma(r,21),green=wma(r,3)).dropna(subset=["rsi","red","green"])
    return out if len(out)>=5 else None
def hm_state(row):
    r,red,g=float(row["rsi"]),float(row["red"]),float(row["green"])
    if r>50 and red<r and g>red: return "BUY-MOMENTUM"
    if r>50 and red<r: return "BUY"
    if r>50: return "TOP-FORMING"
    if r<50 and red>r and g<red: return "SELL-MOMENTUM"
    if red>r: return "SELL"
    return "NO-TRADE"
def is_buy(s): return s in ("BUY","BUY-MOMENTUM")
def is_sell(s): return s in ("SELL","SELL-MOMENTUM")
def newly(bars, side):
    if bars is None or len(bars)<2: return False
    a,b=hm_state(bars.iloc[-1]),hm_state(bars.iloc[-2])
    return (is_buy(a) and not is_buy(b)) if side=="BUY" else (is_sell(a) and not is_sell(b))
def load_symbols():
    for name in ("nse200_symbols.csv","nifty500_symbols.csv"):
        p=DATA/name
        if not p.exists(): continue
        syms=[]
        for line in p.read_text().splitlines()[1:]:
            parts=line.split(",")
            if name.startswith("nse200"):
                s=parts[0].strip()
                if s and s!="Symbol": syms.append(s.replace(".NS",""))
            elif len(parts)>=4 and parts[3].strip()=="EQ":
                syms.append(parts[2].strip())
        if syms: return syms[:200]
    return ["RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","ITC","LT"]
def fetch_daily(sym):
    try:
        df=yf.download(f"{sym}.NS",period="2y",interval="1d",progress=False,threads=False,auto_adjust=True)
        if df is None or df.empty: return None
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        return df.dropna(subset=["Close"])
    except Exception: return None
def fetch_1h(sym):
    try:
        df=yf.download(f"{sym}.NS",period="60d",interval="1h",progress=False,threads=False,auto_adjust=True)
        if df is None or df.empty: return None
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        return df.dropna(subset=["Close"])
    except Exception: return None
def weekly_hm(df):
    if df is None or len(df)<80: return None
    w=df.resample("W-FRI").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna(subset=["Close"])
    return attach_hm(w)
def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {"version":1,"next_id":1,"trades":[]}
def save_state(st):
    STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    STATE_FILE.write_text(json.dumps(st,indent=1,ensure_ascii=False)+"\n")
FIELDS=["id","symbol","side","setup","signal_date","entry_date","entry_time","entry","qty","invested","rsi_entry","sl","tgt","status","exit_date","exit_time","exit","reason","pnl","htf_weekly","htf_daily","notes"]
def save_csv(trades):
    TRADES_FILE.parent.mkdir(parents=True,exist_ok=True)
    with TRADES_FILE.open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore"); w.writeheader(); w.writerows(trades)
def manage_open(trades, moment):
    for t in [x for x in trades if x.get("status")=="OPEN"]:
        h1=attach_hm(fetch_1h(t["symbol"]))
        if h1 is None or len(h1)<2: continue
        last=h1.iloc[-1]; px=float(last["Close"]); hi,lo=float(last["High"]),float(last["Low"])
        t["ltp"]=round(px,2); sign=1 if t["side"]=="BUY" else -1
        t["live_pnl"]=round(sign*(px-float(t["entry"]))*float(t["qty"]),2)
        exit_px=reason=None
        if moment.time()>=EOD: exit_px,reason=px,"EOD"
        elif t["side"]=="BUY":
            if lo<=float(t["sl"]): exit_px,reason=float(t["sl"]),"SL"
            elif hi>=float(t["tgt"]): exit_px,reason=float(t["tgt"]),"TGT"
            elif float(last["rsi"])>=RSI_BOOK: exit_px,reason=px,"RSI86"
            elif is_sell(hm_state(last)): exit_px,reason=px,"HM-FLIP"
        else:
            if hi>=float(t["sl"]): exit_px,reason=float(t["sl"]),"SL"
            elif lo<=float(t["tgt"]): exit_px,reason=float(t["tgt"]),"TGT"
            elif is_buy(hm_state(last)): exit_px,reason=px,"HM-FLIP"
        if exit_px is not None:
            pnl=round(sign*(exit_px-float(t["entry"]))*float(t["qty"])-COST*float(t["invested"]),2)
            t.update(status="CLOSED",exit=round(exit_px,2),reason=reason,pnl=pnl,
                     exit_date=moment.date().isoformat(),exit_time=moment.strftime("%H:%M"))
            try:
                import telegram_notify
                telegram_notify.notify(f"{'✅' if pnl>0 else '🛑'} CLOSE · Intraday HM ({reason})\n{t['side']} {t['symbol']} P&L ₹{pnl:,.0f}")
            except Exception: pass
def run(output: Path):
    moment=now(); today=moment.date(); state=load_state(); trades=state.get("trades") or []
    manage_open(trades, moment)
    entered=0
    def publish():
        # publish current marks/book — the heavy entry scan must never block
        # P&L updates (rate-limited scans froze the P&L column for a day)
        opens=[t for t in trades if t.get("status")=="OPEN"]
        closed=[t for t in trades if t.get("status")=="CLOSED"]
        wins=[t for t in closed if float(t.get("pnl") or 0)>0]
        out={"date":today.isoformat(),"updated_at":now().strftime("%d %b %Y %H:%M:%S IST"),
             "desk":"Intraday HM",
             "rule":("Intraday HM — HTF bias (weekly+daily) then 1H entry. "
                     "BUY: weekly&daily BUY → 1H momentum. SELL: weekly&daily SELL → 1H sell-zone. "
                     "EOD 15:15 · SL 3×1H · 1.5R · RSI86/HM-FLIP. ₹10k · max 5 · max 3/day · NSE-200."),
             "book":{"capital":CAPITAL,"open":len(opens),"invested":round(sum(float(t.get("invested") or 0) for t in opens),2),
                     "realized":round(sum(float(t.get("pnl") or 0) for t in closed),2),
                     "wins":len(wins),"losses":len(closed)-len(wins),
                     "win_rate":round(len(wins)/len(closed)*100,1) if closed else 0.0},
             "signals":[{"symbol":t["symbol"],"side":t["side"],"weekly":t.get("htf_weekly"),"daily":t.get("htf_daily"),"setup":t.get("setup")} for t in opens],
             "monthly_watchlist":[],"open":opens,"closed":closed[-80:],"entered_this_run":entered}
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(out,indent=1,ensure_ascii=False)+"\n")
        return out
    save_state(state); save_csv(trades); publish()   # marks live before the heavy scan
    if moment.weekday()<5 and dtime(9,30)<=moment.time()<EOD:
        open_n=sum(1 for t in trades if t.get("status")=="OPEN")
        taken=sum(1 for t in trades if t.get("entry_date")==today.isoformat() and t.get("status") in ("OPEN","CLOSED"))
        open_syms={t["symbol"] for t in trades if t.get("status")=="OPEN"}
        for i,sym in enumerate(load_symbols()):
            if open_n+entered>=MAX_OPEN or taken+entered>=MAX_NEW: break
            if sym in open_syms: continue
            try:
                daily=fetch_daily(sym)
                d_hm=attach_hm(daily); w_hm=weekly_hm(daily)
                if d_hm is None or w_hm is None: continue
                w_st,d_st=hm_state(w_hm.iloc[-1]),hm_state(d_hm.iloc[-1])
                bias=None
                if is_buy(w_st) and is_buy(d_st): bias="BUY"
                elif is_sell(w_st) and is_sell(d_st): bias="SELL"
                if not bias: continue
                h1=attach_hm(fetch_1h(sym))
                if h1 is None or not newly(h1,bias): continue
                last=h1.iloc[-1]; entry=float(last["Close"])
                if bias=="BUY":
                    sl=float(h1["Low"].iloc[-3:].min())*0.998; risk=entry-sl
                    if risk<=0 or risk/entry>0.03: continue
                    tgt=entry+RR*risk; setup="IHM-BUY"
                else:
                    sl=float(h1["High"].iloc[-3:].max())*1.002; risk=sl-entry
                    if risk<=0 or risk/entry>0.03: continue
                    tgt=entry-RR*risk; setup="IHM-SELL"
                qty=int(LOT/entry)
                if qty<1: continue
                tid=int(state.get("next_id",1)); state["next_id"]=tid+1
                trades.append({"id":tid,"symbol":sym,"side":bias,"setup":setup,
                    "signal_date":today.isoformat(),"entry_date":today.isoformat(),
                    "entry_time":moment.strftime("%H:%M"),"entry":round(entry,2),"qty":qty,
                    "invested":round(qty*entry,2),"rsi_entry":round(float(last["rsi"]),1),
                    "sl":round(sl,2),"tgt":round(tgt,2),"status":"OPEN","exit_date":"","exit_time":"",
                    "exit":0.0,"reason":"","pnl":0.0,"htf_weekly":w_st,"htf_daily":d_st,
                    "notes":f"HTF {w_st}/{d_st} → 1H {setup}","ltp":round(entry,2),"live_pnl":0.0})
                open_syms.add(sym); entered+=1
                print(f"  ENTER {bias} {sym} @{entry:.1f}", flush=True)
                try:
                    import telegram_notify
                    telegram_notify.notify(f"⚡ OPEN · Intraday HM\n{bias} {sym} qty {qty}\nentry ₹{entry:,.2f} · {setup}")
                except Exception: pass
            except Exception: pass
            if (i+1)%25==0: time.sleep(0.4)
    out=publish()
    return out
if __name__=="__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("--output",default=str(OUT_DEFAULT)); a=ap.parse_args()
    res=run(Path(a.output)); b=res["book"]
    print(f"Intraday-HM: open {b['open']}/{MAX_OPEN} invested Rs{b['invested']:,.0f} | entered {res.get('entered_this_run',0)}", flush=True)
