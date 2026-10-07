#!/usr/bin/env bash
# ui-v2.sh — turn the optional new look of the whole portal on or off.
#   bash ui-v2.sh on [push]    add the UI-V2 block to every page, commit (and push)
#   bash ui-v2.sh off [push]   remove the block from every page, commit (and push)
#                              <- the one-command revert
# The look lives in static/ui-v2.css + static/ui-v2.js (scoped under
# html.ui-v2); nothing else in the repo is edited. Per-visitor escape:
# ?classic=1 or the "Classic view" button.
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-}"; PUSH="${2:-}"
[ -f paper.html ] || { echo "Run this from the My-world repo folder (paper.html not found)."; exit 1; }

V="${UI_V2_VERSION:-2}"

case "$ACTION" in
  on)
    [ -f static/ui-v2.css ] && [ -f static/ui-v2.js ] || { echo "static/ui-v2.css / ui-v2.js missing."; exit 1; }
    git tag -f ui-v2-before >/dev/null 2>&1 || true     # safety bookmark of the classic version
    UI_V2_VERSION="$V" python3 - <<'PY'
import os, glob
v = os.environ.get("UI_V2_VERSION", "2")
block = f'''<!-- UI-V2 START (undo with: bash ui-v2.sh off) -->
  <script>try{{var q=new URLSearchParams(location.search);if(q.get("classic")==="1")localStorage.setItem("ui_v2_off","1");if(q.get("classic")==="0")localStorage.removeItem("ui_v2_off");if(localStorage.getItem("ui_v2_off")!=="1")document.documentElement.classList.add("ui-v2")}}catch(e){{document.documentElement.classList.add("ui-v2")}}</script>
  <link rel="stylesheet" href="static/ui-v2.css?v={v}">
  <script defer src="static/ui-v2.js?v={v}"></script>
  <!-- UI-V2 END -->
'''
n_on = n_v = 0
for f in glob.glob("**/*.html", recursive=True):
    if os.sep + ".git" + os.sep in os.path.abspath(f) + os.sep:
        continue
    s = open(f, encoding="utf-8").read()
    if "UI-V2 START" in s:
        s2 = s.replace("ui-v2.css?v=1", f"ui-v2.css?v={v}") \
              .replace("ui-v2.js?v=1", f"ui-v2.js?v={v}")
        if s2 != s:
            open(f, "w", encoding="utf-8").write(s2)
            n_v += 1
        continue
    i = s.find("</head>")
    if i == -1:
        continue
    s = s[:i] + "  " + block + s[i:]
    open(f, "w", encoding="utf-8").write(s)
    n_on += 1
print(f"block added to {n_on} pages, {n_v} existing pages refreshed to v={v}")
PY
    git add -A -- '*.html' static/ui-v2.css static/ui-v2.js ui-v2.sh
    git diff --cached --quiet || git commit -m "all pages: optional ui-v2 look (undo: bash ui-v2.sh off)"
    echo "New look is ON for every page."
    ;;
  off)
    python3 - <<'PY'
import re, glob, os
n_off = 0
pat = re.compile(r"[ \t]*<!-- UI-V2 START.*?<!-- UI-V2 END -->\n", re.S)
for f in glob.glob("**/*.html", recursive=True):
    if os.sep + ".git" + os.sep in os.path.abspath(f) + os.sep:
        continue
    s = open(f, encoding="utf-8").read()
    s2 = pat.sub("", s)
    if s2 != s:
        open(f, "w", encoding="utf-8").write(s2)
        n_off += 1
print(f"block removed from {n_off} pages")
PY
    git add -A -- '*.html'
    git diff --cached --quiet || git commit -m "all pages: remove ui-v2 look (back to classic)"
    echo "Back to the classic look on every page."
    ;;
  *) echo "Usage: bash ui-v2.sh on|off [push]"; exit 1 ;;
esac

if [ "$PUSH" = "push" ]; then git push && echo "Pushed. GitHub Pages updates in about a minute."
else echo "Not pushed yet. Run: git push   (or re-run with 'push')"; fi
