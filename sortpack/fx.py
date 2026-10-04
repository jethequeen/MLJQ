# -*- coding: utf-8 -*-
"""
USD→CAD conversion using the Bank of Canada Valet API (series FXUSDCAD): the official
daily noon rate, no API key. We ask for a short window ending on the wanted date and take
the most recent published observation on/before it (the series has no weekend/holiday
values, so the lookback skips those gaps). Same source boxoffice-quebec uses, so MLJQ and
boxoffice book US money at identical rates.

If the API is unreachable we fall back to the static config.USD_TO_CAD so a run never fails
outright — the caller can see source == 'config' and decide whether to trust it.
"""

import json
import datetime
import urllib.request

from . import config

_VALET = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"


def get_usd_cad_rate(date=None, lookback_days=10, timeout=20):
    """(rate, rate_date, source) — USD→CAD for `date` (yyyy-mm-dd) or the most recent
    business day before it. source is 'boc' (Bank of Canada) or 'config' (static fallback)."""
    try:
        end_d = datetime.date.fromisoformat(date) if date else datetime.date.today()
    except (TypeError, ValueError):
        end_d = datetime.date.today()
    start = (end_d - datetime.timedelta(days=lookback_days)).isoformat()
    url = f"{_VALET}?start_date={start}&end_date={end_d.isoformat()}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for obs in reversed(data.get("observations") or []):
            try:
                v = float(obs.get("FXUSDCAD", {}).get("v"))
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v, obs.get("d"), "boc"
    except Exception:
        pass
    return float(config.USD_TO_CAD), None, "config"
