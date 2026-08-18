#!/usr/bin/env bash
# ============================================================
# Mahi Portal — One-command deploy to GitHub Pages
#
# Usage:
#   ./deploy.sh                  # Build + push to GitHub
#   ./deploy.sh --build-only     # Build without pushing
#   ./deploy.sh --push-only      # Push existing site/ without rebuild
#
# Prerequisites:
#   - GitHub repo created (e.g. mahi-portal)
#   - git remote configured: git remote add origin git@github.com:USER/mahi-portal.git
#   - GitHub Pages enabled (serves from root or docs/ folder)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Default config
OUTPUT_DIR="site"
REMOTE_NAME="origin"
BRANCH="main"
COMMIT_MSG="portal update $(date +%Y-%m-%dT%H:%M)"

# Parse args
BUILD=true
PUSH=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build-only) PUSH=false; shift ;;
    --push-only)  BUILD=false; shift ;;
    -o|--output)  OUTPUT_DIR="$2"; shift 2 ;;
    *)            echo "Unknown option: $1"; exit 1 ;;
  esac
done

# --- Step 1: Build ---
if $BUILD; then
  echo "=== Building static site ==="
  python3 build.py -o "$OUTPUT_DIR"
  echo "=== Build complete: $(find "$OUTPUT_DIR" -name '*.html' | wc -l | tr -d ' ') HTML pages ==="
fi

# --- Step 2: Deploy ---
if $PUSH; then
  # Check if we're in a git repo with remote
  if ! git remote get-url "$REMOTE_NAME" &>/dev/null; then
    echo "❌ No git remote '$REMOTE_NAME' configured."
    echo "   Run: git remote add $REMOTE_NAME git@github.com:USER/mahi-portal.git"
    exit 1
  fi

  echo "=== Deploying to GitHub ==="

  # If OUTPUT_DIR is a subdirectory (like docs/), use it as Pages root
  # Otherwise, copy everything to the repo root
  if [[ "$OUTPUT_DIR" != "." && "$OUTPUT_DIR" != "./" ]]; then
    # Check if this is a repo dedicated to the portal (site/ at root)
    # or a docs/ deployment in an existing repo
    if [[ -f "$OUTPUT_DIR/index.html" ]]; then
      echo "Copying site contents to repo root for GitHub Pages..."
      # Copy all generated files to root (preserving .git)
      rsync -a --exclude='.git' "$OUTPUT_DIR/" ./
      git add -A
    else
      echo "❌ No index.html found in $OUTPUT_DIR. Build may have failed."
      exit 1
    fi
  else
    git add -A
  fi

  # Check if there are changes to commit
  if git diff --cached --quiet; then
    echo "✅ No changes to push. Site is up to date."
  else
    git commit -m "$COMMIT_MSG"
    echo "=== Pushing to $REMOTE_NAME/$BRANCH ==="
    git push "$REMOTE_NAME" "$BRANCH"
    echo "=== Deploy complete ==="
    echo "   Site will be live in ~30 seconds at your GitHub Pages URL."
  fi
fi
