#!/usr/bin/env python3
"""Kite Connect quote feed — TOTP auto-login, no daily manual step.

Layer: PRIMARY for NSE quotes in the cloud engine (cloud_papertrade.py);
Angel SmartAPI and then Yahoo sit behind it. Everything runs on GitHub
Actions — no Mac dependency.

Credentials (env wins — that's how Actions passes repo secrets; a local
gitignored scanners/kite.json with the same keys also works):
  KITE_API_KEY, KITE_API_SECRET        from developers.kite.trade
  KITE_USER_ID, KITE_PASSWORD          the Zerodha login
  KITE_TOTP_SECRET                     the 2FA seed (Zerodha profile ->
                                       TOTP setup; the authenticator secret)

Login flow (once per day; token cached in data/.kite_token.json):
  POST kite.zerodha.com/api/login        (user_id, password)  -> request_id
  POST kite.zerodha.com/api/twofa        (totp, twofa_type)   -> session
  GET  kite login_url with that session  -> redirect carries request_token
  generate_session(request_token, secret) -> access_token (valid till the
  ~07:30 IST flush next morning)

Instruments are cached daily too (data/.kite_instruments.json) so the
3 req/s quote budget is spent on quotes, not lookups. Every failure mode
returns an empty dict / raises — the engine's chain just falls through,
so a missing secret today changes nothing until the keys arrive.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TOKEN_CACHE = DATA / ".kite_token.json"
INSTR_CACHE = DATA / ".kite_instruments.json"
IST = ZoneInfo("Asia/Kolkata")


def _cfg() -> dict:
    import os
    cfg = {k: os.environ.get(v, "").strip() for k, v in {
        "api_key": "KITE_API_KEY", "api_secret": "KITE_API_SECRET",
        "user_id": "KITE_USER_ID", "password": "KITE_PASSWORD",
        "totp": "KITE_TOTP_SECRET"}.items()}
    if all(cfg.values()):
        return cfg
    local = ROOT / "scanners" / "kite.json"
    if local.exists():
        try:
            d = json.loads(local.read_text())
            for k in cfg:
                cfg[k] = cfg[k] or str(d.get(k) or "")
        except Exception:
            pass
    return cfg


def available() -> bool:
    return all(_cfg().values())


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _kite():
    from kiteconnect import KiteConnect
    cfg = _cfg()
    kite = KiteConnect(api_key=cfg["api_key"])
    kite.set_access_token(_access_token(kite, cfg))
    return kite


def _access_token(kite, cfg: dict) -> str:
    cached = {}
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
        except Exception:
            cached = {}
    if cached.get("date") == _today() and cached.get("token"):
        return cached["token"]

    import pyotp
    import requests
    s = requests.Session()
    s.headers.update({"X-Kite-Version": "3", "User-Agent": "Mozilla/5.0"})
    r1 = s.post("https://kite.zerodha.com/api/login",
                data={"user_id": cfg["user_id"], "password": cfg["password"]})
    r1.raise_for_status()
    request_id = r1.json()["data"]["request_id"]
    r2 = s.post("https://kite.zerodha.com/api/twofa",
                data={"user_id": cfg["user_id"], "request_id": request_id,
                      "twofa_type": "totp",
                      "totp": pyotp.TOTP(cfg["totp"]).now()})
    r2.raise_for_status()
    r3 = s.get(kite.login_url(), allow_redirects=True, timeout=15)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(r3.url).query)
    request_token = (qs.get("request_token") or [""])[0]
    if not request_token:
        raise RuntimeError("no request_token on the redirect — login failed")
    token = kite.generate_session(request_token, api_secret=cfg["api_secret"])
    token = token["access_token"] if isinstance(token, dict) else token
    DATA.mkdir(exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(
        {"date": _today(), "token": token}) + "\n")
    return token


def _instrument_tokens(kite) -> dict[str, int]:
    cached = {}
    if INSTR_CACHE.exists():
        try:
            cached = json.loads(INSTR_CACHE.read_text())
        except Exception:
            cached = {}
    if cached.get("date") == _today() and cached.get("map"):
        return cached["map"]
    m = {}
    for row in kite.instruments("NSE"):
        if row.get("instrument_type") == "EQ" and not row.get("expired", False):
            ts = row.get("tradingsymbol", "")
            if ts.isupper() and len(ts) <= 20:
                m[ts] = row["instrument_token"]
    DATA.mkdir(exist_ok=True)
    INSTR_CACHE.write_text(json.dumps(
        {"date": _today(), "map": m}) + "\n")
    return m


def quotes(symbols: list[str]) -> dict[str, dict]:
    """Day-OHLC quotes in the engine's shape: open/high/low/ltp/volume/
    prev_close. Raises on failure so the feed chain falls through."""
    kite = _kite()
    tokens = _instrument_tokens(kite)
    out: dict[str, dict] = {}
    batch = [f"NSE:{s}" for s in symbols if s in tokens]
    for i in range(0, len(batch), 150):
        resp = kite.quote(batch[i:i + 150]) or {}
        for key, q in resp.items():
            sym = key.split(":", 1)[1]
            ohlc = q.get("ohlc") or {}
            o, h, low = (ohlc.get(k) for k in ("open", "high", "low"))
            ltp, pc = q.get("last_price"), ohlc.get("close")
            if None in (o, h, low, ltp, pc) or not ltp:
                continue
            out[sym] = {"open": float(o), "high": float(h), "low": float(low),
                        "ltp": float(ltp), "volume": float(q.get("volume") or 0),
                        "prev_close": float(pc)}
        if i + 150 < len(batch):
            time.sleep(0.4)          # respect the 3 req/s quote budget
    return out
