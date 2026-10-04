# -*- coding: utf-8 -*-
"""
Read BrickStore's cached BrickLink price guide (priceguide_cache.sqlite, table
`pg(id, data)`), offline. Each `data` blob is a fixed 160-byte record:

    int    quantities[Time][Condition]         bytes  0-15   (2x2 int32)
    int    lots      [Time][Condition]         bytes 16-31   (2x2 int32)
    double prices    [Time][Condition][Price]  bytes 32-159  (2x2x4 float64, USD)

    Time      = {PastSix=0, Current=1}     (last-6-months sales vs current for-sale)
    Condition = {New=0, Used=1}
    Price     = {Min=0, Avg=1, QtyAvg=2, Max=3}   (Lowest, Average, WAverage, Highest)

So e.g. current New average = prices[1][0][1] at byte 32 + 9*8 = 104 — the same value
the reference `selling_ratios.py` reads. The cache is only as fresh as the last time
BrickStore updated prices online (check the sqlite file's mtime).
"""

import os
import struct
import sqlite3

from . import config

# (time, condition) index helpers
PAST_SIX, CURRENT = 0, 1
NEW, USED = 0, 1
MIN, AVG, QTY_AVG, MAX = 0, 1, 2, 3

_BLOB_LEN = 160
_PRICES_BASE = 32


def _price_off(time, cond, price):
    return _PRICES_BASE + (((time * 2) + cond) * 4 + price) * 8


class PriceRecord:
    """Decoded 160-byte price blob. All prices are USD; 0.0 means 'no data'."""
    __slots__ = ("_prices", "_qty", "_lots")

    def __init__(self, blob):
        self._qty = struct.unpack_from("<4i", blob, 0)      # [PS.N, PS.U, Cur.N, Cur.U]
        self._lots = struct.unpack_from("<4i", blob, 16)
        self._prices = struct.unpack_from("<16d", blob, 32)

    def price(self, time, cond, kind):
        return self._prices[((time * 2) + cond) * 4 + kind]

    def qty(self, time, cond):
        return self._qty[(time * 2) + cond]

    def lots(self, time, cond):
        return self._lots[(time * 2) + cond]

    # convenience accessors used by the value engine / UI
    @property
    def current_new_min(self):     return self.price(CURRENT, NEW, MIN)
    @property
    def current_new_avg(self):     return self.price(CURRENT, NEW, AVG)
    @property
    def current_new_qtyavg(self):  return self.price(CURRENT, NEW, QTY_AVG)
    @property
    def current_new_max(self):     return self.price(CURRENT, NEW, MAX)
    @property
    def sixmonth_new_min(self):    return self.price(PAST_SIX, NEW, MIN)
    @property
    def sixmonth_new_avg(self):    return self.price(PAST_SIX, NEW, AVG)
    @property
    def sixmonth_new_qtyavg(self): return self.price(PAST_SIX, NEW, QTY_AVG)
    @property
    def current_used_avg(self):    return self.price(CURRENT, USED, AVG)

    # quantities that drive the sell ratio (see setvalue.weighted_ratio / velocity)
    @property
    def current_qty_new(self):     return self.qty(CURRENT, NEW)   # pieces listed for sale
    @property
    def past_six_qty_new(self):    return self.qty(PAST_SIX, NEW)  # pieces sold, last 6 mo

    @property
    def ratio_new(self):
        """Sold-last-6-months / currently-listed. High = sells faster than it stocks."""
        c = self.current_qty_new
        return (self.past_six_qty_new / c) if c > 0 else None


# The price metrics we surface everywhere (value engine, CLI, GUI, sheet columns).
# name -> (time, condition, price-kind)
METRICS = {
    "current_new_min":     (CURRENT, NEW, MIN),
    "current_new_avg":     (CURRENT, NEW, AVG),
    "current_new_qtyavg":  (CURRENT, NEW, QTY_AVG),
    "current_new_max":     (CURRENT, NEW, MAX),
    "sixmonth_new_avg":    (PAST_SIX, NEW, AVG),
    "sixmonth_new_qtyavg": (PAST_SIX, NEW, QTY_AVG),
    "current_used_avg":    (CURRENT, USED, AVG),
}
# stable column order for reports
METRIC_ORDER = ["current_new_min", "current_new_avg", "current_new_qtyavg",
                "current_new_max", "sixmonth_new_avg", "sixmonth_new_qtyavg",
                "current_used_avg"]

# human labels (French, to match the MLJQ interface)
METRIC_LABELS = {
    "current_new_min":     "Neuf min (actuel)",
    "current_new_avg":     "Neuf moyenne (actuel)",
    "current_new_qtyavg":  "Neuf moy. pondérée (actuel)",
    "current_new_max":     "Neuf max (actuel)",
    "sixmonth_new_avg":    "Neuf moyenne (6 mois vendus)",
    "sixmonth_new_qtyavg": "Neuf moy. pondérée (6 mois)",
    "current_used_avg":    "Usagé moyenne (actuel)",
}


class PriceGuide:
    def __init__(self, db_path=None, preload=False):
        self.db_path = db_path or config.BRICKSTORE_PRICEGUIDE
        self._conn = sqlite3.connect(self.db_path)
        self._cache = {}          # key -> PriceRecord | None
        self._all = None          # dict key -> PriceRecord when preloaded
        if preload:
            self._preload()

    @property
    def updated_mtime(self):
        """Unix mtime of the cache file — i.e. how fresh the prices are."""
        return os.path.getmtime(self.db_path)

    def _preload(self):
        self._all = {}
        for key, blob in self._conn.execute("SELECT id, data FROM pg"):
            if len(blob) == _BLOB_LEN:
                self._all[key] = PriceRecord(blob)

    def get(self, key):
        """PriceRecord for a 'P3001@5@B0'-style key, or None if absent."""
        if self._all is not None:
            return self._all.get(key)
        if key in self._cache:
            return self._cache[key]
        row = self._conn.execute(
            "SELECT data FROM pg WHERE id=?", (key,)).fetchone()
        rec = PriceRecord(row[0]) if row and len(row[0]) == _BLOB_LEN else None
        self._cache[key] = rec
        return rec

    def iter_all(self):
        """Yield (key, PriceRecord) for every row — used to snapshot part price history."""
        for key, blob in self._conn.execute("SELECT id, data FROM pg"):
            if len(blob) == _BLOB_LEN:
                yield key, PriceRecord(blob)

    def close(self):
        self._conn.close()


def parse_key(key):
    """'P3001@5@B0' -> ('P', '3001', 5). Returns None if the key is malformed."""
    parts = key.split("@")
    if len(parts) < 2 or not parts[0]:
        return None
    typed_id, color = parts[0], parts[1]
    try:
        return typed_id[0], typed_id[1:], int(color)
    except (ValueError, IndexError):
        return None
