#!/usr/bin/env python3
"""Inject INR P&L rendering into HM BTC/Gold JS — creates tables at runtime."""
from pathlib import Path
import re
p = Path("paper.html")
html = p.read_text()
if "book.open_pnl_inr" in html and "hmbPnlInject" in html:
    print("already injected")
    raise SystemExit(0)

# Find the hmbStatusV / hmbStates pair flexibly
pat = re.compile(
    r'document\.getElementById\("hmbStatusV"\)\.textContent = esc\(d\.status \|\| "[^"]*"\);\s*'
    r'document\.getElementById\("hmbStates"\)\.textContent = String\(d\.signals \|\| 0\);'
)
m = pat.search(html)
if not m:
    raise SystemExit("js anchor not found")

inject = r'''document.getElementById("hmbStatusV").textContent = esc(d.status || "—");
        /* hmbPnlInject */
        var book = d.book || {}, opens = d.open || [], closed = d.closed || [];
        function inrH(n){ if(n==null||n===""||isNaN(Number(n)))return "—"; var v=Number(n); return "₹"+(v>0?"+":"")+v.toLocaleString("en-IN",{maximumFractionDigits:0}); }
        function clsH(n){ n=Number(n); if(isNaN(n)||n===0)return ""; return n>0?"pos":"neg"; }
        var stEl = document.getElementById("hmbStates");
        if (stEl) stEl.textContent = (book.open!=null?book.open:opens.length) + " open · " + inrH(book.open_pnl_inr);
        var statusFlash = document.getElementById("hmbStatus");
        if (statusFlash) statusFlash.innerHTML = "Desk updated " + esc(d.scanned_at||"") + " · Open P&L <span class='"+clsH(book.open_pnl_inr)+"'>" + inrH(book.open_pnl_inr) + "</span> · Realized <span class='"+clsH(book.realized_pnl_inr)+"'>" + inrH(book.realized_pnl_inr) + "</span> · Net <span class='"+clsH(book.net_pnl_inr)+"'>" + inrH(book.net_pnl_inr) + "</span>";
        var tw = document.getElementById("hmbTableWrap");
        if (tw) {
          var pre = "";
          if (opens.length) {
            pre += "<h3 style='color:var(--green)'>🟢 Open (" + opens.length + ") · P&L ₹</h3><table><thead><tr><th>Asset</th><th>Frame</th><th>Setup</th><th>Side</th><th>Entry</th><th>Mark</th><th>Capital</th><th>P&L (₹)</th><th>%</th></tr></thead><tbody>";
            opens.forEach(function(t){
              pre += "<tr><td><strong>"+esc(t.asset)+"</strong></td><td>"+esc(t.frame)+"</td><td>"+esc(t.setup)+"</td><td>"+esc(t.side)+"</td>"
                +"<td>"+(t.entry_price!=null?Number(t.entry_price).toLocaleString():"—")+"</td>"
                +"<td>"+(t.mark!=null?Number(t.mark).toLocaleString():"—")+"</td>"
                +"<td>₹"+Number(t.capital||0).toLocaleString("en-IN")+"</td>"
                +"<td class='"+clsH(t.pnl_inr)+"'><strong>"+inrH(t.pnl_inr)+"</strong></td>"
                +"<td class='"+clsH(t.pnl_pct)+"'>"+(t.pnl_pct!=null?(Number(t.pnl_pct)>0?"+":"")+Number(t.pnl_pct).toFixed(2)+"%":"—")+"</td></tr>";
            });
            pre += "</tbody></table>";
          }
          if (closed.length) {
            pre += "<h3 style='margin-top:1rem;color:var(--red)'>🔴 Closed (" + closed.length + ") · P&L ₹</h3><table><thead><tr><th>Asset</th><th>Frame</th><th>Setup</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>P&L (₹)</th></tr></thead><tbody>";
            closed.slice().reverse().forEach(function(t){
              var win=Number(t.pnl_inr)>0;
              pre += "<tr class='"+(win?"row-win":"row-loss")+"'><td><strong>"+esc(t.asset)+"</strong></td><td>"+esc(t.frame)+"</td><td>"+esc(t.setup)+"</td><td>"+esc(t.side)+"</td>"
                +"<td>"+(t.entry_price!=null?Number(t.entry_price).toLocaleString():"—")+"</td>"
                +"<td>"+(t.exit_price!=null?Number(t.exit_price).toLocaleString():"—")+"</td>"
                +"<td>"+esc(t.exit_reason||"—")+"</td>"
                +"<td class='"+clsH(t.pnl_inr)+"'><strong>"+inrH(t.pnl_inr)+"</strong></td></tr>";
            });
            pre += "</tbody></table>";
          }
          if (pre) tw.insertAdjacentHTML("beforebegin", "<div id='hmbPnlInject'>"+pre+"</div>");
        }
'''

html = html[:m.start()] + inject + html[m.end():]
p.write_text(html)
print("injected", "hmbPnlInject" in html)
