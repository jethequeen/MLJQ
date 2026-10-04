# -*- coding: utf-8 -*-
"""
Compute a set's part-out value, and split the two things that matter for a buy
decision — cleanly separated (they used to be conflated):

  HOW MUCH  you get back  -> recovery_value: the part-out on config.RECOVERY_BASIS
            (sold prices) times a FLAT realization rate. Over the ~5-year horizon
            almost every set realizes roughly the same fraction of its theoretical
            part-out (dregs, erosion), so this is one flat rate on the whole set,
            not a per-part liquidity discount. USD, before the CFB fee.

  HOW FAST  you get there -> weighted_ratio: the qty-weighted sell ratio
            (sold-6mo / listed). Higher = faster. Turned into months_to_liquidate,
            which converts profit into the EXPECTED ANNUAL PROFIT the buy list
            maximizes — the amount, per unit of time capital is tied up.

Everything offline: parts from the catalog blob, prices from the price-guide cache.
"""

from . import priceguide
from . import config
from .priceguide import METRICS, METRIC_ORDER


class SetValue:
    def __init__(self, set_info, totals, lots, priced_lots, total_qty,
                 missing_keys, price_mtime, recovery_value, weighted_ratio):
        self.set_info = set_info               # SetInfo (id, name, years) or None
        self.totals = totals                   # metric name -> summed USD part-out
        self.lots = lots                       # distinct part lots counted
        self.priced_lots = priced_lots         # lots with a current-new-avg price
        self.total_qty = total_qty             # total pieces
        self.missing_keys = missing_keys       # price keys with no data (sample)
        self.price_mtime = price_mtime         # freshness of the price cache
        # HOW MUCH: flat-realization part-out on config.RECOVERY_BASIS (USD, pre-fee).
        self.recovery_value = recovery_value
        # HOW FAST: qty-weighted sell ratio (sold-6mo / listed); higher = faster.
        self.weighted_ratio = weighted_ratio

    @property
    def coverage(self):
        return self.priced_lots / self.lots if self.lots else 0.0


def set_value(catalog, pg, set_no, include_extras=False):
    parts = catalog.inventory(set_no, include_extras=include_extras)
    totals = {m: 0.0 for m in METRIC_ORDER}
    lots = 0
    priced_lots = 0
    total_qty = 0
    wr_num = wr_den = 0.0
    missing = []
    for part in parts:
        lots += 1
        total_qty += part.qty
        rec = pg.get(part.price_key())
        if rec is None:
            if len(missing) < 20:
                missing.append(part.price_key())
            continue
        if rec.current_new_avg > 0:
            priced_lots += 1
        for name, (t, c, k) in METRICS.items():
            totals[name] += part.qty * rec.price(t, c, k)
        ratio = rec.ratio_new                  # sold-6mo / listed, or None
        if ratio:
            wr_num += ratio * part.qty
            wr_den += part.qty
    recovery_value = totals[config.RECOVERY_BASIS] * config.REALIZATION_RATE
    return SetValue(
        set_info=catalog.set_info(set_no),
        totals=totals, lots=lots, priced_lots=priced_lots, total_qty=total_qty,
        missing_keys=missing, price_mtime=pg.updated_mtime,
        recovery_value=recovery_value,
        weighted_ratio=(wr_num / wr_den if wr_den else 0.0))


def market_realization(catalog, pg, set_no):
    """Fraction of the part-out you'd realistically realize (0..1]. Two haircuts per part:
    (1) EROSION — real data: if a part is already listed BELOW what it recently sold for,
        value it at that lower current price (a scarce/unpopular new mould-colour whose
        scarcity price is already slipping); parts listed >= sold keep full value.
    (2) THIN-DEMAND drift — an assumption: a part with very few actual 6-month sales has
        soft demand, so its price won't hold and drifts further down; discount it by how
        far its sales fall below DEMAND_HEALTHY (absolute sales, not the sold/listed ratio,
        which is confounded by market size), capped at VELOCITY_FLOOR. VELOCITY_STRENGTH=0
        disables (2), leaving pure erosion.
    factor = adjusted / base. Returns 1.0 if off or no priced parts. Quantity-independent."""
    if not config.REALIZATION_ADJUST:
        return 1.0
    tm = METRICS[config.RECOVERY_BASIS]          # the sold basis (past-six new avg)
    strength = config.VELOCITY_STRENGTH
    healthy = max(1, config.DEMAND_HEALTHY)
    floor = config.VELOCITY_FLOOR
    base = adjusted = 0.0
    for part in catalog.inventory(set_no):
        rec = pg.get(part.price_key())
        if rec is None:
            continue
        sold = rec.price(*tm)
        if sold <= 0:
            continue
        cur = rec.current_new_avg                # currently-listed average
        price = min(sold, cur) if cur > 0 else sold          # (1) real erosion
        if strength > 0:                                     # (2) predicted thin-demand drift
            health = min(1.0, rec.past_six_qty_new / healthy)
            price *= max(floor, 1.0 - strength * (1.0 - health))
        base += part.qty * sold
        adjusted += part.qty * price
    return adjusted / base if base > 0 else 1.0


def months_to_liquidate(weighted_ratio):
    """Estimated months to liquidate a set from its weighted sell ratio:
    SELL_WINDOW_MONTHS / ratio, clamped to [MIN_SELL_MONTHS, MAX_SELL_MONTHS]."""
    if weighted_ratio and weighted_ratio > 0:
        m = config.SELL_WINDOW_MONTHS / weighted_ratio
    else:
        m = config.MAX_SELL_MONTHS
    return max(config.MIN_SELL_MONTHS, min(config.MAX_SELL_MONTHS, m))
