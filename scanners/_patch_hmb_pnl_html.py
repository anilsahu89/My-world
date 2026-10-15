#!/usr/bin/env python3
from pathlib import Path
p = Path("paper.html")
html = p.read_text()

if "hmbOpenPnl" not in html:
    marker = 'id="hmbStates"'
    if marker not in html:
        raise SystemExit("hmbStates missing")
    # insert summary cards after the Live States sum-card closing div
    live = '<div class="sum-card"><span class="sum-label">Live States Tracked</span><span class="sum-value" id="hmbStates">—</span></div>'
    if live not in html:
        # tolerate any dash in the em-dash slot
        import re
        m = re.search(r'<div class="sum-card"><span class="sum-label">Live States Tracked</span><span class="sum-value" id="hmbStates">.</span></div>', html)
        if not m:
            raise SystemExit("live card not found")
        live = m.group(0)
    extra = (
        '\n    <div class="sum-card"><span class="sum-label">Open</span><span class="sum-value" id="hmbOpenN">—</span></div>'
        '\n    <div class="sum-card"><span class="sum-label">Open P&L (₹)</span><span class="sum-value" id="hmbOpenPnl">—</span></div>'
        '\n    <div class="sum-card"><span class="sum-label">Realized P&L (₹)</span><span class="sum-value" id="hmbRealized">—</span></div>'
        '\n    <div class="sum-card"><span class="sum-label">Net P&L (₹)</span><span class="sum-value" id="hmbNet">—</span></div>'
    )
    html = html.replace(live, live + extra, 1)
    wrap = '<div class="table-wrap" id="hmbTableWrap">'
    if wrap not in html:
        raise SystemExit("hmbTableWrap missing")
    open_block = (
        '  <h3 style="color:var(--green)">🟢 Open positions <span class="muted" id="hmbOpenCount">(0)</span></h3>\n'
        '  <div class="table-wrap" id="hmbOpenWrap"><div class="flash">No open HM BTC/Gold positions.</div></div>\n\n'
        '  <h3 style="margin-top:1.5rem;color:var(--red)">🔴 Closed <span class="muted" id="hmbClosedCount">(0)</span></h3>\n'
        '  <div class="table-wrap" id="hmbClosedWrap"><div class="flash">No closed trades yet.</div></div>\n\n'
        '  '
    )
    html = html.replace(wrap, open_block + wrap, 1)
    print("cards+tables inserted")
else:
    print("cards already present")

needle = 'document.getElementById("hmbStatusV").textContent = esc(d.status || "—");\n        document.getElementById("hmbStates").textContent = String(d.signals || 0);'
if "hmbOpenWrap" in html and "book.open_pnl_inr" in html:
    print("js already patched")
elif needle not in html:
    # try flexible dash
    import re
    m = re.search(r'document\.getElementById\("hmbStatusV"\)\.textContent = esc\(d\.status \|\| "."\);\s*document\.getElementById\("hmbStates"\)\.textContent = String\(d\.signals \|\| 0\);', html)
    if not m:
        raise SystemExit("js needle not found")
    needle = m.group(0)
    do_js = True
else:
    do_js = True

if "book.open_pnl_inr" not in html:
    inject = (
        'document.getElementById("hmbStatusV").textContent = esc(d.status || "—");\n'
        '        var book = d.book || {}, opens = d.open || [], closed = d.closed || [];\n'
        '        function inr(n){ if(n==null||n===""||isNaN(Number(n)))return "—"; var v=Number(n); return "₹"+(v>0?"+":"")+v.toLocaleString("en-IN",{maximumFractionDigits:0}); }\n'
        '        function clsP(n){ n=Number(n); if(isNaN(n)||n===0)return ""; return n>0?"pos":"neg"; }\n'
        '        var oN=document.getElementById("hmbOpenN"); if(oN)oN.textContent=String(book.open!=null?book.open:opens.length);\n'
        '        var opEl=document.getElementById("hmbOpenPnl"); if(opEl){opEl.textContent=inr(book.open_pnl_inr);opEl.className="sum-value "+clsP(book.open_pnl_inr);}\n'
        '        var rzEl=document.getElementById("hmbRealized"); if(rzEl){rzEl.textContent=inr(book.realized_pnl_inr);rzEl.className="sum-value "+clsP(book.realized_pnl_inr);}\n'
        '        var ntEl=document.getElementById("hmbNet"); if(ntEl){ntEl.textContent=inr(book.net_pnl_inr);ntEl.className="sum-value "+clsP(book.net_pnl_inr);}\n'
        '        var oc=document.getElementById("hmbOpenCount"); if(oc)oc.textContent="("+opens.length+")";\n'
        '        var ow=document.getElementById("hmbOpenWrap");\n'
        '        if(ow){ if(!opens.length) ow.innerHTML=\'<div class="flash">No open HM BTC/Gold positions.</div>\';\n'
        '          else { var h="<table><thead><tr><th>Asset</th><th>Frame</th><th>Setup</th><th>Side</th><th>Entry</th><th>Mark</th><th>Capital</th><th>P&L (₹)</th><th>%</th></tr></thead><tbody>";\n'
        '            opens.forEach(function(t){ h+="<tr><td><strong>"+esc(t.asset)+"</strong></td><td>"+esc(t.frame)+"</td><td>"+esc(t.setup)+"</td><td>"+esc(t.side)+"</td>"\n'
        '              +"<td>"+(t.entry_price!=null?Number(t.entry_price).toLocaleString():"—")+"</td>"\n'
        '              +"<td>"+(t.mark!=null?Number(t.mark).toLocaleString():"—")+"</td>"\n'
        '              +"<td>₹"+Number(t.capital||0).toLocaleString("en-IN")+"</td>"\n'
        '              +\'<td class="\'+clsP(t.pnl_inr)+\'"><strong>\'+inr(t.pnl_inr)+"</strong></td>"\n'
        '              +\'<td class="\'+clsP(t.pnl_pct)+\'">\'+(t.pnl_pct!=null?(Number(t.pnl_pct)>0?"+":"")+Number(t.pnl_pct).toFixed(2)+"%":"—")+"</td></tr>"; });\n'
        '            ow.innerHTML=h+"</tbody></table>"; } }\n'
        '        var cc=document.getElementById("hmbClosedCount"); if(cc)cc.textContent="("+closed.length+")";\n'
        '        var cw=document.getElementById("hmbClosedWrap");\n'
        '        if(cw){ if(!closed.length) cw.innerHTML=\'<div class="flash">No closed trades yet.</div>\';\n'
        '          else { var h2="<table><thead><tr><th>Asset</th><th>Frame</th><th>Setup</th><th>Side</th><th>Entry</th><th>Exit</th><th>Reason</th><th>P&L (₹)</th></tr></thead><tbody>";\n'
        '            closed.slice().reverse().forEach(function(t){ var win=Number(t.pnl_inr)>0; h2+=\'<tr class="\'+(win?"row-win":"row-loss")+\'"><td><strong>\'+esc(t.asset)+"</strong></td><td>"+esc(t.frame)+"</td><td>"+esc(t.setup)+"</td><td>"+esc(t.side)+"</td>"\n'
        '              +"<td>"+(t.entry_price!=null?Number(t.entry_price).toLocaleString():"—")+"</td>"\n'
        '              +"<td>"+(t.exit_price!=null?Number(t.exit_price).toLocaleString():"—")+"</td>"\n'
        '              +"<td>"+esc(t.exit_reason||"—")+"</td>"\n'
        '              +\'<td class="\'+clsP(t.pnl_inr)+\'"><strong>\'+inr(t.pnl_inr)+"</strong></td></tr>"; });\n'
        '            cw.innerHTML=h2+"</tbody></table>"; } }\n'
    )
    html = html.replace(needle, inject, 1)
    print("js patched")

p.write_text(html)
print("ok", "hmbOpenPnl" in html)
