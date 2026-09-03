"""
refresh_kite_token.py
----------------------
Run this ONCE each trading morning (takes ~60 seconds). It:
  1. Opens the Kite login URL in your browser
  2. You log in + complete 2FA (this step can't be automated — SEBI rule)
  3. You paste the redirect URL back into this script
  4. It exchanges the request_token for a fresh access_token
  5. It pushes that access_token into your GitHub repo as a secret
     (KITE_ACCESS_TOKEN), so every scheduled GitHub Action run today
     can use it without you touching anything else.

SETUP (one-time):
  pip install kiteconnect pynacl requests

  Set these environment variables before running (or hardcode them
  locally in a .env you keep OUT of git — never commit secrets):
    KITE_API_KEY        - from developer.kite.trade
    KITE_API_SECRET      - from developer.kite.trade
    GITHUB_PAT           - a GitHub Personal Access Token with
                            "repo" scope (Settings > Developer settings
                            > Personal access tokens)
    GITHUB_REPO           - e.g. "anilsahu89/My-world"

USAGE:
  python refresh_kite_token.py
"""

import base64
import os
import sys

import requests
from kiteconnect import KiteConnect
from nacl import encoding, public

API_KEY = os.environ["KITE_API_KEY"]
API_SECRET = os.environ["KITE_API_SECRET"]
GITHUB_PAT = os.environ["GITHUB_PAT"]
GITHUB_REPO = os.environ["GITHUB_REPO"]  # "owner/repo"
SECRET_NAME = "KITE_ACCESS_TOKEN"


def get_access_token() -> str:
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    print("\n1. Open this URL and log in to Kite:\n")
    print(f"   {login_url}\n")
    print("2. After login, you'll land on a redirect URL (it may show")
    print("   an error page — that's fine, you just need the URL).")
    print("3. Paste the FULL redirect URL below.\n")

    redirect_url = input("Paste redirect URL here: ").strip()

    # request_token is a query param on the redirect URL
    if "request_token=" not in redirect_url:
        sys.exit("Couldn't find request_token in that URL — try again.")
    request_token = redirect_url.split("request_token=")[1].split("&")[0]

    session = kite.generate_session(request_token, api_secret=API_SECRET)
    return session["access_token"]


def push_secret_to_github(token_value: str) -> None:
    """Encrypts and uploads token_value as a GitHub Actions repo secret."""
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
    }

    # 1. Get the repo's public key (needed to encrypt the secret)
    key_resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
        headers=headers,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()

    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(token_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    # 2. Upload the encrypted secret
    put_resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{SECRET_NAME}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
    )
    put_resp.raise_for_status()
    print(f"\n✅ {SECRET_NAME} updated in {GITHUB_REPO}. Today's scans are good to go.")


if __name__ == "__main__":
    token = get_access_token()
    push_secret_to_github(token)
