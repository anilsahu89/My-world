# Proposed RVOL gate change (15 Oct 2026)

## Why
Zero O=L/O=H paper trades since **1 Oct 2026**. Root cause was the volume gate:
- On Angel/Kite feeds, `vol_known` was never set → every pick rejected as `no_vol_data`
- The 15 Oct RVOL rebuild fixed attachment, but thresholds are still a bit high for early session

## Change (in `scanners/cloud_papertrade.py`)

```python
# Before
NSE_RVOL_OL = 1.5              # O=L long
NSE_RVOL_OH = 1.2              # O=H short

# After
NSE_RVOL_OL = 1.0              # O=L long  (lowered — restart flow, tune from vol_audit)
NSE_RVOL_OH = 0.8              # O=H short (lowered — restart flow, tune from vol_audit)
```

## How to apply (30 seconds in GitHub UI)
1. Open: https://github.com/anilsahu89/My-world/edit/main/scanners/cloud_papertrade.py
2. Find lines with `NSE_RVOL_OL = 1.5` and `NSE_RVOL_OH = 1.2`
3. Change to `1.0` and `0.8` as above
4. Commit to `main` (or this branch)

## After merge
- Next market-hours NSE poll (09:20–09:45 IST) should take O=L/O=H trades again
- Inspect `vol_audit` in `paper_ol.json` / state and raise thresholds if too many low-quality fills

## Note
Do **not** merge branch `fix/lower-rvol-gates` — it contains a broken partial `cloud_papertrade.py` from an interrupted push. Delete that branch.
