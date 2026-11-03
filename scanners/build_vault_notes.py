#!/usr/bin/env python3
"""build_vault_notes.py — readable note bodies for the Vault Globe drawer.

Walks the note dirs (strategy/, concepts/, sources/, trade-reviews/), maps
every graph.json node URL to its real file (handling the /strategies/ vs
/strategy/ plural mismatch and genuinely missing pages), extracts readable
text, and writes data/vault_notes.json keyed by graph node id.

The dashboard's note drawer reads this file — no more dead note links.
Runs on the relay watchdog cycle; notes change only on deploy.
"""
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "data" / "graph.json"
OUT = ROOT / "data" / "vault_notes.json"
CAP = 4000  # characters of body text per note
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "svg", "noscript"}
BREAK_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "tr", "div", "section", "br", "table"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip > 0:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip == 0 and data.strip():
            self.parts.append(data.strip())


def extract_text(path: Path) -> str:
    try:
        raw = path.read_text(errors="ignore")
    except OSError:
        return ""
    # keep only the note body: <article class="md"> > <main> > whole page
    m = re.search(r"<article[\s\S]*?</article>", raw, re.I) or \
        re.search(r"<main[\s\S]*?</main>", raw, re.I)
    if m:
        raw = m.group(0)
    raw = re.sub(r'<div id="settingsModal"[\s\S]*?(?=<div id=|</main>|$)', "", raw)
    parser = _Text()
    try:
        parser.feed(raw)
    except Exception:
        pass
    text = "\n".join(p for p in parser.parts if p)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text[:CAP]


def resolve(real_url: str):
    # /strategies/ (plural) holds stale duplicates — prefer the current
    # singular dirs (strategy/, concepts/, sources/, trade-reviews/)
    rel = real_url.lstrip("/")
    for cand in (rel.replace("strategies/", "strategy/", 1), rel):
        p = ROOT / cand
        if p.exists():
            return p
    return None


def main() -> int:
    graph = json.loads(GRAPH.read_text())
    nodes = {}
    missing = []
    for n in graph.get("nodes", []):
        entry = {"title": n.get("title"), "category": n.get("category"),
                 "url": n.get("url"), "updated": n.get("updated"),
                 "live": bool(n.get("live")), "path": None, "text": ""}
        p = resolve(n.get("url", ""))
        if p is not None:
            entry["path"] = str(p.relative_to(ROOT))
            entry["text"] = extract_text(p)
        else:
            missing.append(n.get("url"))
        nodes[n["id"]] = entry
    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "notes": len(nodes), "missing": len(missing), "nodes": nodes}
    OUT.write_text(json.dumps(out, indent=0, ensure_ascii=False) + "\n")
    print(f"vault_notes.json: {len(nodes)} notes, {len(missing)} without a page "
          f"({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
