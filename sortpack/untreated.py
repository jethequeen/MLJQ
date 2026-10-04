# -*- coding: utf-8 -*-
"""
L'inventaire NON TRAITÉ — la valeur marchande des sets qui sont chez nous.

Pourquoi
--------
La colonne *Inventaire MLJQ* des « Résultats mensuels » ne comptait que ce que le portail CFB
affiche. Or un set acheté, trié, emballé mais pas encore saisi par CFB n'est nulle part : ni
en encaisse (il est payé), ni au portail (il n'y est pas). Résultat, acheter de l'inventaire
faisait *baisser* la Fortune, donc le delta, donc le budget du mois suivant — on se punissait
d'investir. C'est le trou que cette valeur comble.

Ce qu'on compte
---------------
**Seulement ce qui est chez nous** : la table `inventory` de l'historique, c'est-à-dire les
exemplaires achetés et pas encore expédiés. Un set part de cette table au moment où il est
marqué *Livré* (`history.send_to_cfb`), donc il quitte le « non traité » exactement quand il
entre dans la file de saisie de CFB. Pas de double compte, pas de trou.

Sur quelle base
---------------
`part-out sur les prix VENDUS des 6 derniers mois × REALIZATION_RATE`, converti en CAD au taux
du jour (Banque du Canada). C'est-à-dire la valeur de revente attendue **avant** la commission
CFB — elle n'est pas due tant que rien n'est vendu. C'est la base choisie le 2026-09-30, et la
seule qui soit cohérente avec la colonne V (Binobrick), qui est elle aussi une valeur de
marchandise et non un net après frais.

Le prix vient du cache BrickStore, donc cette valeur bouge avec lui. Elle est indicative :
elle dit l'ordre de grandeur de ce qu'on détient, pas un prix de vente ferme.
"""

import datetime

from . import config


def _rate():
    """(taux USD→CAD, date, source). Le live d'abord, le config.USD_TO_CAD en secours."""
    try:
        from . import fx
        return fx.get_usd_cad_rate()
    except Exception:
        return float(config.USD_TO_CAD), None, "config"


def per_set(hist=None, cat=None, pg=None):
    """[{set_id, qty, lots, pieces, usd, cad}, …] — un set en stock par ligne.

    `qty` est le nombre d'exemplaires possédés ; `lots` et `pieces` sont ceux d'UNE copie
    (le lot ne se multiplie pas, cf. la règle du Journal), `pieces` est déjà à l'échelle ×qty.
    """
    from .history import HistoryDB
    from .setvalue import set_value
    own = hist or HistoryDB()
    try:
        rows = list(own._conn.execute("SELECT set_id, qty FROM inventory ORDER BY set_id"))
    finally:
        if hist is None:
            own.close()
    if not rows:
        return []

    if cat is None:
        from . import catalogdb
        cat = catalogdb.shared()
    if pg is None:
        from .priceguide import PriceGuide
        pg = PriceGuide()

    rate, _d, _s = _rate()
    out = []
    for set_id, qty in rows:
        num = str(set_id).split("-")[0]
        try:
            sv = set_value(cat, pg, num)
        except Exception:
            continue                      # set inconnu du catalogue : on ne devine pas
        qty = int(qty or 0)
        usd = sv.recovery_value * qty     # part-out 6 mois × REALIZATION_RATE, par copie
        parts = cat.inventory(num, include_extras=True)
        out.append({
            "set_id": str(set_id),
            "name": sv.set_info.name if sv.set_info else "",
            "qty": qty,
            "lots": sv.lots,
            "pieces": sum(p.qty for p in parts) * qty,
            "coverage": (sv.priced_lots / sv.lots) if sv.lots else 0.0,
            "usd": round(usd, 2),
            "cad": round(usd * rate, 2),
        })
    return out


def value(hist=None, cat=None, pg=None):
    """(valeur CAD, pièces, détail) de tout ce qui est chez nous. (0, 0, []) si rien."""
    rows = per_set(hist=hist, cat=cat, pg=pg)
    return (round(sum(r["cad"] for r in rows), 2),
            sum(r["pieces"] for r in rows),
            rows)


def report(log=print):
    """Ce qu'on détient, set par set — la colonne W du mois en cours."""
    total, pieces, rows = value()
    rate, rdate, rsrc = _rate()
    log("Inventaire non traité (ce qui est chez nous) — taux %.4f (%s, %s)"
        % (rate, rdate or "?", rsrc))
    for r in rows:
        log("   %-10s ×%-3d %5d lots %7d pièces  %10.2f $  %s"
            % (r["set_id"], r["qty"], r["lots"], r["pieces"], r["cad"], r["name"][:28]))
    log("   → %d set(s), %d pièces, %.2f $ CAD" % (len(rows), pieces, total))
    return total, pieces, rows


def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    report()


if __name__ == "__main__":
    main()
