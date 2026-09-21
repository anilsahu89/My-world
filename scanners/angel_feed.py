#!/usr/bin/env python3
"""Angel One SmartAPI live-quote provider (free market data, TOTP auto-login).

Credentials resolution order:
  1. Environment: ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD,
     ANGEL_TOTP_SECRET           (GitHub Actions secrets)
  2. ~/ZCodeProject/papertrade_ol/angel_credentials.json   (Mac testing —
     never inside this repo, which is public)

Usage:
  python3 angel_feed.py            # self-test: login + quote a few names
  from angel_feed import quotes    # quotes(["SBIN","TCS",...]) -> dict

Quote payload per symbol (same shape the engine gets from Yahoo):
  {"open", "high", "low", "ltp", "volume", "prev_close"}
open/high/low are the live cumulative day values; volume is cumulative.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pyotp
import requests

BASE_URL = "https://apiconnect.angelone.in"
LOCAL_CREDS = Path("/Users/apple/ZCodeProject/papertrade_ol/angel_credentials.json")
BATCH = 45                     # quote API accepts ~50 symbols per call
_login_cache: dict = {}        # jwt per process; one login per run is enough


def _creds() -> dict:
    env = {k: os.environ.get(f"ANGEL_{k}") for k in
           ("API_KEY", "CLIENT_CODE", "PASSWORD", "TOTP_SECRET")}
    if all(env.values()):
        return {"api_key": env["API_KEY"], "client_code": env["CLIENT_CODE"],
                "password": env["PASSWORD"], "totp_secret": env["TOTP_SECRET"]}
    if LOCAL_CREDS.exists():
        return json.loads(LOCAL_CREDS.read_text())
    return {}


def available() -> bool:
    c = _creds()
    return bool(c.get("api_key") and c.get("client_code")
                and c.get("password") and c.get("totp_secret"))


def _headers(api_key: str, jwt: str | None = None) -> dict:
    h = {"Content-Type": "application/json",
         "Accept": "application/json",
         "X-UserType": "USER",
         "X-SourceID": "WEB",
         "X-ClientLocalIP": "127.0.0.1",
         "X-ClientPublicIP": "127.0.0.1",
         "X-MACAddress": "00:00:00:00:00:00",
         "X-PrivateKey": api_key}
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h


def login(max_tries: int = 3) -> str:
    """Headless daily login: client code + password + TOTP -> JWT."""
    if "jwt" in _login_cache:
        return _login_cache["jwt"]
    c = _creds()
    if not c:
        raise RuntimeError("no Angel credentials (env or "
                           + str(LOCAL_CREDS) + ")")
    body = {"clientcode": c["client_code"], "password": c["password"],
            "totp": pyotp.TOTP(c["totp_secret"]).now()}
    last = None
    for attempt in range(max_tries):
        r = requests.post(
            f"{BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword",
            headers=_headers(c["api_key"]), json=body, timeout=20)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200 and data.get("status") and \
                data.get("data", {}).get("jwtToken"):
            _login_cache["jwt"] = data["data"]["jwtToken"]
            return _login_cache["jwt"]
        # a TOTP generated right before a 30s window flips is rejected once
        last = f"HTTP {r.status_code}: {data.get('message') or data.get('errorcode') or r.text[:120]}"
        time.sleep(2)
        body["totp"] = pyotp.TOTP(c["totp_secret"]).now()
    raise RuntimeError(f"Angel login failed for {c['client_code']}: {last}")


def _token_map(symbols: list[str]) -> dict[str, str]:
    """symbol -> Angel exchange token via the scrip master (cached per day)."""
    import datetime as _dt
    cache = Path(f"/tmp/angel_scrip_master_{_dt.date.today().isoformat()}.json")
    nse: dict[str, str] = {}
    if cache.exists():
        nse = json.loads(cache.read_text())
    else:
        r = requests.get("https://margincalculator.angelbroking.com"
                         "/OpenAPI_File/files/OpenAPIScripMaster.json",
                         timeout=90)
        r.raise_for_status()
        nse = {row["symbol"]: row["token"] for row in r.json()
               if row.get("exch_seg") == "NSE"
               and row.get("symbol", "").endswith("-EQ")}
        try:
            cache.write_text(json.dumps(nse))
        except OSError:
            pass
    return {s: nse[f"{s}-EQ"] for s in symbols if f"{s}-EQ" in nse}


def quotes(symbols: list[str]) -> dict[str, dict]:
    """Batch live day-OHLC quotes for NSE EQ symbols. Throws on hard failure
    — the engine falls back to Yahoo when it does."""
    c = _creds()
    jwt = login()
    tok = _token_map([s.strip().upper() for s in symbols])
    out: dict[str, dict] = {}
    names = list(tok)
    for i in range(0, len(names), BATCH):
        chunk = names[i:i + BATCH]
        r = requests.post(
            f"{BASE_URL}/rest/secure/angelbroking/market/v1/quote/",
            headers=_headers(c["api_key"], jwt),
            json={"mode": "FULL",
                  "exchangeTokens": {"NSE": [tok[s] for s in chunk]}},
            timeout=25)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Angel quote response not JSON "
                               f"(HTTP {r.status_code})")
        if not data.get("status"):
            raise RuntimeError(f"Angel quote error: "
                               f"{data.get('message') or data}")
        for row in data.get("data", {}).get("fetched", []):
            sym = row.get("tradingSymbol", "").replace("-EQ", "").upper()
            if not sym:
                continue
            def _f(k, cast=float):
                v = row.get(k)
                return cast(v) if v not in (None, "") else None
            out[sym] = {
                "open": _f("open"), "high": _f("high"),
                "low": _f("low"), "ltp": _f("ltp"),
                "volume": _f("tradeVolume", float) or 0.0,
                "prev_close": _f("close"),
            }
        if i + BATCH < len(names):
            time.sleep(0.35)          # stay far under the rate limit
    # drop rows missing the OHLC backbone (pre-open / not traded)
    return {s: q for s, q in out.items()
            if None not in (q["open"], q["high"], q["low"], q["ltp"])}


def _selftest() -> int:
    c = _creds()
    if not c:
        print("no credentials found — fill angel_credentials.json "
              "or set ANGEL_* env vars")
        return 1
    print(f"login as {c['client_code']} ...")
    jwt = login()
    print("login OK (jwt", jwt[:18] + "…)")
    q = quotes(["SBIN", "TCS", "RELIANCE", "INFY", "HDFCBANK"])
    for s, row in q.items():
        print(f"  {s:10} O {row['open']:>9.2f} H {row['high']:>9.2f} "
              f"L {row['low']:>9.2f} LTP {row['ltp']:>9.2f} "
              f"vol {row['volume']:>12,.0f}")
    print(f"{len(q)} quotes OK — feed healthy")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
