#!/usr/bin/env python3
"""Best-effort Telegram alerts for every paper-trade open/close.

Config (either works; env wins — that's how GitHub Actions passes repo
secrets):
  * env TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  * scanners/telegram.json  {"bot_token": "...", "chat_id": 123...}
    (gitignored — local ticks use it)

Setup (one time):
  1. In Telegram, talk to @BotFather -> /newbot -> copy the token.
  2. Put it in scanners/telegram.json: {"bot_token": "123:ABC"}
  3. Open YOUR new bot in Telegram and send it /start (bots cannot DM
     first — the chat only exists after you write to it).
  4. Run:  python3 scanners/telegram_notify.py
     -> auto-detects your chat id from the /start, writes it into the
        json, sends a test alert. Done.
  5. For the cloud desks (run on GitHub Actions) add the same two values
     as repo secrets TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.

Every call degrades silently when unconfigured — a missing token can
never break a trading tick.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG_FILE = ROOT / "scanners" / "telegram.json"


def _cfg() -> tuple[str | None, str | None]:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and chat:
        return tok, chat
    if CFG_FILE.exists():
        try:
            d = json.loads(CFG_FILE.read_text())
            return (tok or d.get("bot_token") or None,
                    chat or str(d.get("chat_id") or "") or None)
        except Exception:
            return None, None
    return tok or None, chat or None


def configured() -> bool:
    tok, chat = _cfg()
    return bool(tok and chat)


_TRADE_HEADER = re.compile(r"\b(OPEN|CLOSE|RE-ENTRY(?: \d+)?)\s*·")


def is_trade_event(text: str) -> bool:
    """True only for a trade OPEN / CLOSE / RE-ENTRY message (header line
    looks like '⚡ OPEN · desk' or '✅ CLOSE · desk (reason)')."""
    lines = (text or "").strip().splitlines()
    return bool(lines and _TRADE_HEADER.search(lines[0]))


def notify(text: str, force: bool = False) -> bool:
    """Send one TRADE alert. Never raises; returns success.

    Owner rule (15 Oct 2026): Telegram is for trades only — a message goes
    out when a paper trade is OPENED or CLOSED, nothing else. Feed
    warnings, scan failures and status text are printed to the run log
    instead (force=True is only for the one-off setup test message).

    Duplicate guard: the same trade event (header + symbol/qty line) is sent
    once per day. If a run loses its ledger write (cancelled job / git
    reset) and re-fires the same OPEN or CLOSE, it is muted rather than
    re-sent. A hard cap of 40 sends/day remains as a last-resort backstop."""
    if not force and not is_trade_event(text):
        print("telegram: non-trade message kept in log only -> "
              + (text or "").strip().splitlines()[0][:120]
              if (text or "").strip() else "telegram: empty message skipped")
        return False
    tok, chat = _cfg()
    if not tok or not chat:
        return False
    import hashlib
    import time as _t
    state_p = ROOT / "data" / ".tg_state.json"
    try:
        state = json.loads(state_p.read_text())
    except Exception:
        state = {}
    now = _t.time()
    today = _t.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state = {"day": today, "count": 0, "suppressed": 0}
    # trade-event identity = first two lines (event header + side/symbol/qty),
    # NOT the full text: prices, P&L and times differ on a re-fired duplicate
    ident = "\n".join(text.strip().splitlines()[:2])
    h = "h_" + hashlib.md5(ident.encode()).hexdigest()
    last = state.get(h) or 0
    if now - last < 20 * 3600:           # same trade event today -> mute
        state[h] = now
        state["suppressed"] = state.get("suppressed", 0) + 1
        try:
            state_p.write_text(json.dumps(state))
        except OSError:
            pass
        return True
    if state.get("count", 0) >= 40:      # daily hard cap
        state["suppressed"] = state.get("suppressed", 0) + 1
        try:
            state_p.write_text(json.dumps(state))
        except OSError:
            pass
        return True
    state[h] = now
    state["count"] = state.get("count", 0) + 1
    try:
        state_p.write_text(json.dumps(state))
    except OSError:
        pass
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text,
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"telegram notify failed: {e}")
        return False


def _detect_and_test() -> int:
    """Read getUpdates, capture the chat that talked to the bot, test-send."""
    cfg = {}
    if CFG_FILE.exists():
        try:
            cfg = json.loads(CFG_FILE.read_text())
        except Exception:
            cfg = {}
    tok = cfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        print("no bot_token in scanners/telegram.json (or env) — see the "
              "setup steps in this file's docstring.")
        return 1
    if not cfg.get("chat_id"):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/getUpdates")
            with urllib.request.urlopen(req, timeout=10) as r:
                ups = json.load(r).get("result", [])
        except Exception as e:
            print(f"getUpdates failed: {e}")
            return 1
        chats = {}
        for u in ups:
            m = u.get("message") or u.get("edited_message") or {}
            c = m.get("chat") or {}
            if c.get("id"):
                chats[c["id"]] = (c.get("username") or c.get("first_name")
                                  or "?")
        if not chats:
            print("No chat found yet — open your new bot in Telegram and "
                  "send it /start, then run this again.")
            return 1
        print("chats seen:")
        for cid, name in chats.items():
            print(f"  {cid}  (@{name})")
        cfg["chat_id"] = list(chats)[0]
        CFG_FILE.write_text(json.dumps(cfg, indent=1) + "\n")
        print(f"chat_id {cfg['chat_id']} saved to {CFG_FILE.name}")
    ok = notify("✅ Mahi paper-trade alerts are LIVE.\n"
                "Every open/close from the desks lands here.", force=True)
    print("test send:", "sent" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_detect_and_test())
