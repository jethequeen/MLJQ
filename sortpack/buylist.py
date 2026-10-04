# -*- coding: utf-8 -*-
"""
Rank discounted sets by "should I buy this to part out?".

For each set in a discount feed (Amazon: ID, Theme, Price CAD, Discount):

    net_recovery_CAD = recovery_value_USD * USD_TO_CAD * (1 - CFB_FEE)
    cost_CAD         = price_CAD * (1 + tax)          # tax only if INCLUDE_PURCHASE_TAX
    ROI              = net_recovery_CAD / cost_CAD     # how much comes back
    months           = SELL_WINDOW / weighted_ratio   # how fast it liquidates
    annual_return    = (ROI - 1) * 12 / months        # <-- THE objective: $ profit / $ / year
    annual_profit    = (net_recovery - cost) * 12 / months   # same, in absolute $/year

recovery_value is the FLAT-realization part-out on RECOVERY_BASIS (sold prices) — the
amount is roughly set-independent; speed is the differentiator. Candidates clearing
ROI_FLOOR (and MIN_COVERAGE) are ranked by CAPITAL EFFICIENCY = profit per dollar per
year (so a cheap fast flip beats a big slow one), then a monthly batch is filled greedily
within MONTHLY_BUDGET_CAD — a standout (ROI >= STANDOUT_ROI, absolute) may use the
flex band up to budget*(1+BUDGET_FLEX). All money is CAD; every threshold is a config knob.
"""

from . import config
from . import setvalue


class Candidate:
    __slots__ = ("set_id", "name", "theme", "source", "url", "price", "discount",
                 "net_recovery", "roi", "profit", "max_buy", "coverage", "year",
                 "ratio", "months", "annual_profit", "annual_return", "chosen",
                 "qty", "lot_cost", "annual_profit_total", "realization",
                 "pieces_per_copy", "pieces")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        self.chosen = False


def rank(cat, pg, offers, exclude_ids=None, budget=None):
    """Rank a discount feed. `offers` is an iterable of (set_id, theme, price_cad,
    discount, source, url[, bonus]) rows — e.g. from HistoryDB.latest_feed(); `bonus` is
    a comma-joined list of bonus set numbers bundled in (Costco). `exclude_ids` is a
    set of canonical set ids to drop (already bought recently). Returns
    (candidates_sorted, skipped): candidates clearing the ROI floor, sorted by profit
    per dollar per year, with `.chosen` set on the selected monthly batch."""
    exclude_ids = exclude_ids or set()
    rate = config.USD_TO_CAD
    keep = 1 - config.CFB_FEE
    tax = config.PURCHASE_TAX_RATE if config.INCLUDE_PURCHASE_TAX else 0.0
    skipped = []                       # (set_no, reason)
    # One candidate per REAL offer — keep the actual store and its actual price (a set
    # sold at both Amazon and Costco yields two rows, source "amazon" and "costco"; no
    # merge, no "both"). The valuation only depends on the set, so cache it per set.
    valcache = {}                      # canonical set_id -> SetValue
    dfcache = {}                        # canonical set_id -> price-erosion realization
    piececache = {}                    # canonical set_id -> pieces per copy

    def _copies(src):
        """Combien d'exemplaires on achète, selon le magasin (voir BUY_QTY_BY_SOURCE)."""
        by = getattr(config, "BUY_QTY_BY_SOURCE", None) or {}
        return max(1, int(by.get((src or "").lower(), config.TARGET_QTY_PER_SET)))

    def _pieces(set_no):
        """Pièces d'un exemplaire, d'après le catalogue — la même référence que verify,
        extras compris, puisqu'on les catalogue et qu'ils partent avec l'envoi."""
        k = cat.set_info(set_no).set_id
        n = piececache.get(k)
        if n is None:
            try:
                n = sum(p.qty for p in cat.inventory(set_no, include_extras=True))
            except Exception:
                n = 0
            piececache[k] = n
        return n

    def _value(set_no):
        k = cat.set_info(set_no).set_id
        v = valcache.get(k)
        if v is None:
            v = setvalue.set_value(cat, pg, set_no)
            valcache[k] = v
        return v

    def _df(set_no):
        k = cat.set_info(set_no).set_id
        d = dfcache.get(k)
        if d is None:
            d = setvalue.market_realization(cat, pg, set_no)
            dfcache[k] = d
        return d

    cands = []
    for row in offers:
        set_no, theme, price, disc, source, url = row[:6]
        bonus = row[6] if len(row) > 6 else ""
        if not cat.has_set(set_no):
            skipped.append((set_no, "hors catalogue"))
            continue
        if price <= 0:
            skipped.append((set_no, "prix invalide"))
            continue
        key = cat.set_info(set_no).set_id
        if key in exclude_ids:
            skipped.append((set_no, "acheté récemment"))
            continue
        v = _value(set_no)
        if v.coverage < config.MIN_COVERAGE:
            skipped.append((set_no, f"couverture {v.coverage:.0%}"))
            continue
        # a Costco bundle recovers the main set PLUS its bonus set(s) for the one price;
        # each set's recovery is haircut by its own price-erosion realization factor.
        raw_recovery = v.recovery_value
        recovery_usd = v.recovery_value * _df(set_no)
        bonus_ids = []
        for b in (bonus or "").split(","):
            b = b.strip()
            if not b or not cat.has_set(b):
                continue
            bv = _value(b)
            raw_recovery += bv.recovery_value
            recovery_usd += bv.recovery_value * _df(b)
            bonus_ids.append(b)
        realization = recovery_usd / raw_recovery if raw_recovery else 1.0
        net = recovery_usd * rate * keep          # realization + erosion, net of fee, CAD
        cost = price * (1 + tax)
        roi = net / cost
        months = setvalue.months_to_liquidate(v.weighted_ratio)
        profit = net - cost
        annual_profit = profit * 12.0 / months if months else 0.0
        si = v.set_info
        copies = _copies(source)
        per_copy = _pieces(set_no) + sum(_pieces(b) for b in bonus_ids)
        name = si.name + (" + " + "+".join(bonus_ids) if bonus_ids else "")
        cands.append(Candidate(
            set_id=si.set_id, name=name, theme=theme, source=source, url=url,
            price=price, discount=disc, net_recovery=net, roi=roi, profit=profit,
            max_buy=net / config.ROI_FLOOR if config.ROI_FLOOR else 0.0,
            coverage=v.coverage, year=si.year_from, ratio=v.weighted_ratio,
            months=months, annual_profit=annual_profit,
            annual_return=(roi - 1.0) * 12.0 / months if months else 0.0,
            qty=copies, lot_cost=cost * copies, annual_profit_total=annual_profit * copies,
            realization=realization,
            pieces_per_copy=per_copy, pieces=per_copy * copies))

    cands = [c for c in cands if c.roi >= config.ROI_FLOOR]
    cands.sort(key=lambda c: c.annual_return, reverse=True)   # profit per $ per year
    _select_batch(cands, config.MONTHLY_BUDGET_CAD if budget is None else budget)
    return cands, skipped


def open_batch_target():
    """Ce qu'il reste à trouver pour compléter l'envoi en cours.

    Un batch déjà entamé ne demande plus 20 000 pièces mais ce qui lui manque : le Flower
    Wall (8 163 pièces) dans le batch 1 laisse 11 837 pièces à acheter, pas 20 000. Sans ça
    on achèterait de quoi faire un envoi complet par-dessus un envoi à moitié plein, et le
    surplus resterait à dormir jusqu'au batch suivant.

    Quand plusieurs batchs sont ouverts, on complète **le plus avancé** : c'est celui qui
    partira le plus tôt. Renvoie (nom, pièces déjà là, pièces à trouver)."""
    target = int(getattr(config, "BATCH_TARGET_PIECES", 20000))
    try:
        from . import batch as _batch
        best = None
        for name in sorted(_batch.all_batches()):
            st = _batch.status(name)
            if st["closed"] or st["full"]:
                continue
            if best is None or st["pieces"] > best[1]:
                best = (name, st["pieces"])
        if best:
            return best[0], best[1], max(0, target - best[1])
    except Exception:
        pass                           # pas de registre lisible : on vise un envoi entier
    return None, 0, target


def _select_batch(cands, budget, target_pieces=None):
    """Descend le classement jusqu'à réunir assez de PIÈCES pour compléter l'envoi.

    **Le plancher de pièces prime sur le budget.** Un envoi incomplet immobilise le tri et
    fait attendre le client, alors qu'un dépassement de budget ne fait qu'avancer une dépense
    qu'on aurait faite le mois suivant. `MONTHLY_BUDGET_CAD`, `BUDGET_FLEX`, `STANDOUT_ROI` et
    `BATCH_SIZE` cessent donc d'être des plafonds : ils deviennent informatifs, et le rapport
    dit ce que le lot coûte et de combien il dépasse.

    `ROI_FLOOR` et les règles de valeur, elles, ne cèdent jamais — `cands` a déjà été filtré
    en amont. On ne descend pas en qualité pour remplir un envoi : si les candidats rentables
    n'y suffisent pas, on prend tout ce qu'il y a et le manque est signalé.

    Un set déjà pris dans l'autre magasin n'est jamais repris."""
    if target_pieces is None:
        target_pieces = open_batch_target()[2]
    spent, pieces, n = 0.0, 0, 0
    picked = set()                     # canonical set ids already in the batch
    for c in cands:                    # already sorted, best first
        if pieces >= target_pieces:
            break
        if c.set_id in picked:
            continue
        c.chosen = True
        spent += c.lot_cost
        pieces += c.pieces or 0
        n += 1
        picked.add(c.set_id)
    return spent
