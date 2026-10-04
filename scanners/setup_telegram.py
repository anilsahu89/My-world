#!/usr/bin/env python3
"""One-command Telegram alert setup for all paper-trade desks.

You do TWO things in Telegram (2 minutes):
  1. Talk to @BotFather -> /newbot -> name it -> copy the token (123456:ABC-...)
  2. Open YOUR new bot in Telegram and send it any message (/start)

Then run:
    python3 setup_telegram.py <paste-the-bot-token-here>

The script:
  * auto-detects your chat id from the message you sent the bot
  * writes scanners/telegram.json (gitignored — local ticks use it)
  * sends you a test alert right away
  * pushes TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID into the My-world repo's
    encrypted GitHub Actions secrets (the cloud desks read them there)
"""
import json
import subprocess
import urllib.parse
import sys
import urllib.request
from pathlib import Path

SITE = Path("/Users/apple/ZCodeProject/my-world-publish")
REPO = "anilsahu89/My-world"
CFG = SITE / "scanners" / "telegram.json"
API = "https://api.telegram.org/bot{token}/{meth}"


def token() -> str:
    url = subprocess.run(["git", "-C", SITE, "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return url.split("@")[0].rsplit(":", 1)[1]


def tg(token_: str, meth: str) -> dict:
    with urllib.request.urlopen(API.format(token=token_, meth=meth), timeout=15) as r:
        return json.loads(r.read())


def api(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/secrets/{path}",
        method="PUT",
        headers={"Authorization": f"token {token()}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()
    return {}


def main() -> int:
    if len(sys.argv) != 2 or ":" not in sys.argv[1]:
        print(__doc__)
        return 1
    tok = sys.argv[1].strip()
    me = tg(tok, "getMe")
    name = me["result"]["username"]
    print(f"bot OK: @{name}")

    upd = tg(tok, "getUpdates")["result"]
    chat = None
    for u in reversed(upd):
        msg = u.get("message") or u.get("channel_post")
        if msg and msg.get("chat", {}).get("id"):
            chat = msg["chat"]["id"]
            break
    if chat is None:
        print("NO message found on the bot yet — open the bot in Telegram and send /start, then re-run.")
        return 1
    print(f"chat id detected: {chat}")

    CFG.write_text(json.dumps({"bot_token": tok, "chat_id": chat}, indent=1) + "\n")
    print(f"wrote {CFG}")

    test = ("✅ Telegram alerts LIVE — all 11 paper desks (OL/OH, QM, HM, Swing, "
            "Theta, Mill, BTC, Gold, NIFTY LTP, Future Arbitrage) will now ping "
            "every open/close here.")
    urllib.request.urlopen(
        API.format(token=tok, meth="sendMessage").replace(
            "sendMessage", f"sendMessage?chat_id={chat}&text={urllib.parse.quote(test)}"),
        timeout=15).read()
    print("test alert sent")

    from nacl import encoding, public
    gh = {"Authorization": f"token {token()}"}
    pk = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
        headers=gh)).read())
    key = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    sealed = __import__("nacl").public.SealedBox(key)

    def push(name, value):
        body = {"encrypted_value": encoding.Base64Encoder().encode(
            sealed.encrypt(value.encode())).decode(), "key_id": pk["key_id"]}
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/secrets/{name}",
            method="PUT", headers={**gh, "Accept": "application/vnd.github+json",
                                   "Content-Type": "application/json"},
            data=json.dumps(body).encode())
        urllib.request.urlopen(req, timeout=20).read()

    push("TELEGRAM_BOT_TOKEN", tok)
    push("TELEGRAM_CHAT_ID", str(chat))
    print("GitHub secrets set: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
    print("\nDONE — every desk now alerts on open/close, local and cloud.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
