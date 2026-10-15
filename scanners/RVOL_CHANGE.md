# RVOL gate change (15 Oct 2026)

## Why
Zero O=L/O=H paper trades since **1 Oct 2026**. The volume gate rejected every pick (`no_vol_data` / high thresholds).

## Change in `scanners/cloud_papertrade.py`

```
NSE_RVOL_OL = 1.0   # was 1.5
NSE_RVOL_OH = 0.8   # was 1.2
```

Applied by workflow `apply-rvol-fix.yml` on this branch, or manually.

## After merge
Next 09:20\u201309:45 IST window should take O=L/O=H trades. Tune from `vol_audit`.

## Cleanup
Delete branch `fix/lower-rvol-gates` (broken partial file). Do not merge it.
