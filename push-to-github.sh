#!/bin/bash
# === Mahi Portal Deploy Script ===
# Usage: ./push-to-github.sh YOUR_GITHUB_TOKEN
# Your portal will be live at: https://anilsahu89.github.io/My-world/
set -e
TOKEN="$1"
if [ -z "$TOKEN" ]; then
    echo "Usage: ./push-to-github.sh ghp_YOUR_TOKEN_HERE"
    echo "Get a token at: https://github.com/settings/tokens"
    echo "  Generate new token (classic), check: repo, actions"
    exit 1
fi
USER="anilsahu89"
REPO_NAME="My-world"
echo "Checking repo..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $TOKEN" https://api.github.com/repos/$USER/$REPO_NAME)
if [ "$HTTP_STATUS" != "200" ]; then
    echo "Repo not found or token invalid (HTTP $HTTP_STATUS)"
    exit 1
fi
echo "Repo found. Pushing..."
git remote remove origin 2>/dev/null || true
git remote add origin https://$TOKEN@github.com/$USER/$REPO_NAME.git
git branch -M main
git push -u origin main --force 2>&1
git remote set-url origin https://github.com/$USER/$REPO_NAME.git
echo "Enabling GitHub Pages..."
curl -s -o /dev/null -w "Pages: %{http_code}\n" -X POST -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/$USER/$REPO_NAME/pages -d '{"source":{"branch":"main","path":"/"}}' || true
echo ""
echo "DONE! Portal live at: https://anilsahu89.github.io/My-world/"
echo "Settings: token = your ghp_ token, repo = anilsahu89/My-world"
