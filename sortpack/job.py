# -*- coding: utf-8 -*-
"""
Weekly buy-report job, fully headless and DB-only (no CSV): refresh the Amazon
discounts (Brickset), store them in the price history, rank the buy list into the
history DB, and — if configured — POST the stored list to the Google Sheet. The
SQLite is the single source of truth; the sheet POST reads it back.

Called from the CLI (`value.py job`), the GUI button, and the scheduled task.
"""

import datetime
import os

from . import config
# `amazon` et `costco` NE SONT PAS importés ici : ce sont les deux scrapers, et `amazon`
# est le seul module du projet à dépendre de `requests`. Importés en tête, ils rendaient
# `job` inimportable sans cette dépendance — donc « Fin de mois » échouait sur
# « No module named requests », alors qu'il ne lit que la feuille financière (urllib,
# bibliothèque standard). requirements.txt promet exactement l'inverse : « requests +
# beautifulsoup4 ne servent qu'aux scrapers ». Ils sont donc importés là où on scrape,
# dans `run()` — voir plus bas.
from . import buylist
from . import purchases
from . import sheet
from .catalogdb import CatalogDB
from .priceguide import PriceGuide
from .history import HistoryDB


# While the catalog stays stale, re-send the warning banner at most this often (days) so
# a no-change day still nudges you, without daily spam.
_STALE_WARN_EVERY_DAYS = 7


def _next_month(mstr):
    """'2026-08' -> '2026-09' (the month whose budget a given month's delta funds)."""
    y, m = int(mstr[:4]), int(mstr[5:7])
    m += 1
    if m > 12:
        m, y = 1, y + 1
    return f"{y:04d}-{m:02d}"


def _prev_month(mstr):
    """'2026-09' -> '2026-08' (the source month a budget was EARNED from)."""
    y, m = int(mstr[:4]), int(mstr[5:7])
    m -= 1
    if m < 1:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def post_budgets(hist, stored, log=print):
    """Écrit les « Montant à investir » calculés dans le Sommaire mensuel (col G).

    Le budget d'un mois s'affiche sur la ligne du mois qui l'a GAGNÉ (son delta), pas sur
    celui où il se dépense. Idempotent par le méta `budget_posted_<mois>` : un mois n'est
    réécrit que si son montant a changé — ce qui arrive quand la Fortune est corrigée.
    Renvoie le nombre de lignes écrites."""
    if not stored or not config.BUY_REPORT_POST_URL:
        return 0
    ok = 0
    for m, amt in stored:
        src = _prev_month(m)              # la ligne du mois qui a produit ce budget
        key = f"budget_posted_{src}"
        if hist.get_meta(key) == f"{amt:.2f}":
            continue
        try:
            sheet.post_monthly_budget(src, amt)
            hist.set_meta(key, f"{amt:.2f}")
            ok += 1
        except Exception as e:
            log(f"  ⚠ budget {src} non posté : {str(e)[:80]}")
    if ok:
        log(f"  → {ok} montant(s) à investir posté(s) au Sommaire mensuel (col G).")
    return ok


def refresh_budget(hist, log=print, post=False):
    """Read the financial sheet and (re)store every computable monthly budget: for each
    month whose net-worth Delta is known, the NEXT month's budget = delta × MLJQ rate
    (stored only from BUDGET_ANCHOR_MONTH onward). Idempotent. Returns (rate, stored) where
    stored is a sorted list of (month, amount). Raises if the sheet is unreachable.

    `post=True` écrit aussi les montants dans la feuille (voir post_budgets). C'était jadis
    réservé au rapport d'achat quotidien : « Fin de mois » calculait, rangeait, et laissait la
    colonne G vide — on entrait sa Fortune, on cliquait, et rien n'apparaissait là où on
    l'attendait."""
    fin = sheet.fetch_finance()
    rate, deltas = fin.get("rate"), fin.get("deltas") or {}
    stored = []
    if rate:
        today = datetime.date.today().isoformat()
        for delta_month, d in deltas.items():
            if d is None:
                continue
            month = _next_month(delta_month)               # budget for the month AFTER
            if month >= config.BUDGET_ANCHOR_MONTH:
                amount = round(d * rate, 2)
                hist.set_monthly_budget(month, amount, d, rate, delta_month, today)
                stored.append((month, amount))
    stored = sorted(stored)
    if post:
        post_budgets(hist, stored, log=log)
    return rate, stored


def _age_days(path):
    """Age in days of a file, or None if it's missing."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return (datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)).days


def run(post=False, costco_feed=True, log=print):
    """Run the full pipeline, fully headless. Amazon scrapes Brickset; Costco calls its
    JSON API directly (no browser). `post=True` also POSTs to the sheet when configured.
    Returns a summary dict."""
    date = datetime.date.today().isoformat()
    hist = HistoryDB()

    # business knobs come from the sheet's Paramètres tab (fallback: config.py defaults)
    try:
        changed = config.apply_sheet_params(sheet.fetch_params())
        if changed:
            log(f"Paramètres chargés du sheet ({len(changed)} valeurs).")
    except Exception as e:
        log(f"Paramètres du sheet indisponibles, valeurs par défaut ({str(e)[:80]}).")

    # daily CFB cookie liveness check — emails once when a cookie flips to expired
    try:
        from . import cfb_inventory
        log("Vérification des cookies CFB…")
        cfb_inventory.check_and_alert(log=log)
    except Exception as e:
        log(f"  Vérif cookies CFB ignorée ({str(e)[:70]}).")

    log("Chargement du catalogue + guide de prix…")
    cat = CatalogDB()
    pg = PriceGuide(preload=True)

    # freshness reminders: the catalog (new sets) and the price cache (valuations) are
    # each only as fresh as the last BrickStore "Update Database" / "Update Price Guide".
    warnings = []
    cat_age = _age_days(config.BRICKSTORE_DB)
    if cat_age is not None and cat_age > config.CATALOG_STALE_DAYS:
        warnings.append(f"Catalogue BrickStore vieux de {cat_age} j — lancez "
                        f"« Update Database » pour voir les nouveaux sets.")
    pg_age = _age_days(config.BRICKSTORE_PRICEGUIDE)
    if pg_age is not None and pg_age > config.PRICEGUIDE_STALE_DAYS:
        warnings.append(f"Prix BrickStore vieux de {pg_age} j — lancez « Update Price "
                        f"Guide » (et `value.py pricegaps` pour les figurines exclusives).")
    warning = "  ·  ".join(warnings) or None
    log(f"  Fraîcheur : catalogue {cat_age} j, prix {pg_age} j."
        + ("  ⚠ " + warning if warning else ""))

    # sync logged purchases (Journal Catégorie-A) → bought table + CFB folders
    ps = purchases.sync(cat, hist, log=log)
    log(f"  Journal synchronisé : {ps.get('journal', 0)} achat(s) miroir"
        + (f", {ps['new']} nouveau(x) (dossiers CFB créés)" if ps["new"] else "") + ".")

    log("Rafraîchissement des rabais Amazon (Brickset, headless)…")
    from . import amazon          # tire `requests` : seulement quand on scrape vraiment
    amz = amazon.fetch()
    hist.snapshot_amazon(amz, date)
    log(f"  {len(amz)} offres Amazon.")

    n_cos = 0
    if costco_feed:
        log("Rafraîchissement des rabais Costco (API, headless)…")
        try:
            from . import costco
            cos = costco.fetch(is_set=cat.has_set)
            hist.snapshot_costco(cos, date)
            n_cos = len(cos)
            log(f"  {n_cos} offres Costco.")
        except Exception as e:
            log(f"  ⚠ Costco ignoré (erreur : {str(e)[:120]})")

    # dynamic monthly budget from the financial sheet (delta of prior month × MLJQ rate);
    # idempotent, so the flat MONTHLY_BUDGET_CAD is used only if the sheet is unreachable.
    try:
        rate, stored = refresh_budget(hist, log, post=post)
        if rate:
            log(f"Budget dynamique : {len(stored)} mois calculé(s) (taux MLJQ {rate:.0%}).")
    except Exception as e:
        log(f"Budget dynamique indisponible ({str(e)[:80]}) — repli sur le budget fixe.")

    avail, months = hist.available_budget(config.MONTHLY_BUDGET_CAD,
                                          config.BUDGET_ANCHOR_MONTH)
    spent = hist.spent_since(config.BUDGET_ANCHOR_MONTH)
    log(f"Budget : {avail:,.0f}$ dispo (cumul {months} mois × {config.MONTHLY_BUDGET_CAD:,.0f}$ "
        f"depuis {config.BUDGET_ANCHOR_MONTH}, déjà dépensé {spent:,.0f}$).")
    # post the running "reste à investir" (cumulative available) to the dashboard cell
    if post and config.BUY_REPORT_POST_URL:
        try:
            sheet.post_reste_a_investir(round(avail, 2))
            log(f"  → reste à investir ({avail:,.2f}$) posté au tableau de bord "
                f"({config.DASHBOARD_TAB}!{config.DASHBOARD_CELL}).")
        except Exception as e:
            log(f"  ⚠ reste à investir non posté : {str(e)[:80]}")

    log("Classement des candidats (Amazon + Costco, une ligne par offre réelle)…")
    exclude = hist.recent_bought_ids(config.REBUY_WINDOW_MONTHS)
    cands, skipped = buylist.rank(cat, pg, hist.latest_feed(),
                                  exclude_ids=exclude, budget=avail)
    n_reb = sum(1 for _, why in skipped if why == "acheté récemment")
    if n_reb:
        log(f"  {n_reb} offre(s) écartée(s) : set acheté < {config.REBUY_WINDOW_MONTHS} mois.")
    chosen = [c for c in cands if c.chosen]
    # email only when the chosen batch differs from the previous run (daily-run friendly)
    prev = hist.previous_chosen_ids(date)
    changed = prev is None or {c.set_id for c in chosen} != prev
    hist.snapshot_buy_list(cands, date)
    log(f"  {len(cands)} candidats · lot du mois {len(chosen)} sets → historique (buy_list).")

    # a stale catalog forces the email through the change-gate, but only weekly (no spam)
    email = changed
    if warning:
        last = hist.get_meta("last_stale_email")
        stale_due = last is None or (datetime.date.fromisoformat(last)
                                     <= datetime.date.today()
                                     - datetime.timedelta(days=_STALE_WARN_EVERY_DAYS))
        if changed or stale_due:
            email = True
            hist.set_meta("last_stale_email", date)

    posted = False
    if post and config.BUY_REPORT_POST_URL:
        why = ("lot modifié" if changed else
               "catalogue périmé" if email else "lot inchangé — pas de courriel")
        log(f"Envoi au Google Sheet ({why})…")
        # BUY_SHEET_COLS porte desormais `pieces`, calcule au classement avec les extras
        # (la reference de verify et des batchs). On ne le recalcule plus ici : l'ancienne
        # version le faisait avec include_extras=False et donnait donc un autre chiffre que
        # celui qui remplit un envoi.
        from .history import BUY_SHEET_COLS
        reply = sheet.post_buy_list(list(hist.latest_buy_list(chosen_only=True)),
                                    header=list(BUY_SHEET_COLS),
                                    email=email, warning=warning)
        log(f"  {reply}")
        posted = True
    elif post:
        log("  (POST au sheet non configuré — BUY_REPORT_POST_URL vide, ignoré.)")

    hist.close()
    return {
        "date": date,
        "amazon": len(amz),
        "costco": n_cos,
        "candidates": len(cands),
        "chosen": len(chosen),
        "batch_cost": sum(c.lot_cost for c in chosen),
        "batch_annual_profit": sum(c.annual_profit_total for c in chosen),
        "budget_available": avail,
        "posted": posted,
        "catalog_age_days": cat_age,
        "priceguide_age_days": pg_age,
        "warning": warning,
    }
