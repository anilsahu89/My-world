#!/usr/bin/env bash
# ui-v2.sh — turn the optional new look of paper.html on or off.
#   bash ui-v2.sh on [push]    add the 3-line block to paper.html, commit (and push)
#   bash ui-v2.sh off [push]   remove the block again, commit (and push)  <- the one-command revert
# The new look lives in static/ui-v2.css + static/ui-v2.js; nothing else in the repo is edited.
set -euo pipefail
cd "$(dirname "$0")"
ACTION="${1:-}"; PUSH="${2:-}"
[ -f paper.html ] || { echo "Run this from the My-world repo folder (paper.html not found)."; exit 1; }

case "$ACTION" in
  on)
    [ -f static/ui-v2.css ] && [ -f static/ui-v2.js ] || { echo "static/ui-v2.css / ui-v2.js missing."; exit 1; }
    if grep -q "UI-V2 START" paper.html; then echo "Already on."; exit 0; fi
    git tag -f ui-v2-before >/dev/null 2>&1 || true     # safety bookmark of the classic version
    python3 - <<'PY'
import re
p = "paper.html"
s = open(p, encoding="utf-8").read()
block = '''<!-- UI-V2 START (undo with: bash ui-v2.sh off) -->
  <script>try{var q=new URLSearchParams(location.search);if(q.get("classic")==="1")localStorage.setItem("ui_v2_off","1");if(q.get("classic")==="0")localStorage.removeItem("ui_v2_off");if(localStorage.getItem("ui_v2_off")!=="1")document.documentElement.classList.add("ui-v2")}catch(e){document.documentElement.classList.add("ui-v2")}</script>
  <link rel="stylesheet" href="static/ui-v2.css?v=1">
  <script defer src="static/ui-v2.js?v=1"></script>
  <!-- UI-V2 END -->
'''
i = s.find("</head>")
assert i != -1, "no </head> found"
open(p, "w", encoding="utf-8").write(s[:i] + "  " + block + s[i:])
PY
    git add paper.html static/ui-v2.css static/ui-v2.js ui-v2.sh
    git commit -m "paper.html: optional ui-v2 look (undo: bash ui-v2.sh off)" >/dev/null
    echo "New look is ON."
    ;;
  off)
    if ! grep -q "UI-V2 START" paper.html; then echo "Already off."; exit 0; fi
    python3 - <<'PY'
import re
p = "paper.html"
s = open(p, encoding="utf-8").read()
s = re.sub(r"[ \t]*<!-- UI-V2 START.*?<!-- UI-V2 END -->\n", "", s, flags=re.S)
open(p, "w", encoding="utf-8").write(s)
PY
    git add paper.html
    git commit -m "paper.html: remove ui-v2 look (back to classic)" >/dev/null
    echo "Back to the classic look."
    ;;
  *) echo "Usage: bash ui-v2.sh on|off [push]"; exit 1 ;;
esac

if [ "$PUSH" = "push" ]; then git push && echo "Pushed. GitHub Pages updates in about a minute."
else echo "Not pushed yet. Run: git push   (or re-run with 'push')"; fi
