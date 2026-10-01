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


def notify(text: str) -> bool:
    """Send one message. Never raises; returns success."""
    tok, chat = _cfg()
    if not tok or not chat:
        return False
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
                "Every open/close from the desks lands here.")
    print("test send:", "sent" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_detect_and_test())
