#!/bin/bash
# Mac-side dispatch driver for the My-world paper-trade cloud desks.
# Why: GitHub scheduled triggers on this repo drop most slots (watchdog's
# 20-min cron fires 2-4x/day; every slot missed 6 Nov). Dispatch events work
# 100%, so this runs every 5 minutes (launchd) and dispatches each desk slot
# once, deduped via a state file. All times are IST (Mac local).
# Install: launchd agent com.anilsahu.papertrade-dispatcher (StartInterval=300).

STATE="$HOME/.mac_dispatcher_state"
touch "$STATE"
NOW_EPOCH=$(date +%s)
DOW=$(date +%u)          # 1=Mon .. 7=Sun
HHMM=$(date +%H%M)
DATE_TODAY=$(date +%Y-%m-%d)

dispatch() {  # $1 = desk
  local desk="$1"
  TOKEN=$(printf "protocol=https\nhost=github.com\n" | git credential fill 2>/dev/null | grep "^password=" | cut -d= -f2)
  [ -z "$TOKEN" ] && { echo "$(date) no token"; exit 1; }
  CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST \
    -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/anilsahu89/My-world/actions/workflows/cloud-papertrade.yml/dispatches" \
    -d "{\"ref\":\"main\",\"inputs\":{\"desk\":\"$desk\",\"mode\":\"day\"}}")
  echo "$(date) dispatch $desk -> HTTP $CODE"
}

# ---- GC desk: every 15 minutes, 24/7 (piggybacks theta/mill/btc/ltp/arb ticks)
LAST_GC=$(grep "^gc_last=" "$STATE" | tail -1 | cut -d= -f2)
if [ -z "$LAST_GC" ] || [ $((NOW_EPOCH - LAST_GC)) -ge 840 ]; then
  dispatch gc
  echo "gc_last=$NOW_EPOCH" >> "$STATE"
fi

if [ "$DOW" -ge 6 ]; then exit 0; fi   # weekends: GC only

# ---- NSE intraday: ignition 09:16-09:33, entry 09:34-09:51, mgmt 10:34-14:53, square-off 15:37-15:53
NSE_WINDOW=0
if [ "$HHMM" -ge 0916 ] && [ "$HHMM" -le 0951 ]; then NSE_WINDOW=1; fi
if [ "$HHMM" -ge 1034 ] && [ "$HHMM" -le 1453 ]; then NSE_WINDOW=1; fi
if [ "$HHMM" -ge 1537 ] && [ "$HHMM" -le 1553 ]; then NSE_WINDOW=1; fi
LAST_NSE=$(grep "^nse_${DATE_TODAY}=" "$STATE" | tail -1 | cut -d= -f2)
if [ "$NSE_WINDOW" = 1 ]; then
  if [ -z "$LAST_NSE" ] || [ $((NOW_EPOCH - LAST_NSE)) -ge 780 ]; then   # >=13 min
    dispatch nse
    echo "nse_${DATE_TODAY}=$NOW_EPOCH" >> "$STATE"
  fi
fi

# ---- Swing EOD 17:36-17:41 (once per day)
if [ "$HHMM" -ge 1736 ] && [ "$HHMM" -le 1741 ]; then
  if ! grep -q "^swing_${DATE_TODAY}=" "$STATE"; then
    dispatch swing
    echo "swing_${DATE_TODAY}=$NOW_EPOCH" >> "$STATE"
  fi
fi

# ---- ChartLink EOD 17:51-17:56 (once per day)
if [ "$HHMM" -ge 1751 ] && [ "$HHMM" -le 1756 ]; then
  if ! grep -q "^chartlink_${DATE_TODAY}=" "$STATE"; then
    dispatch chartlink
    echo "chartlink_${DATE_TODAY}=$NOW_EPOCH" >> "$STATE"
  fi
fi

# ---- tidy: keep state file small
if [ $(wc -l < "$STATE") -gt 400 ]; then
  tail -200 "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
fi
exit 0
