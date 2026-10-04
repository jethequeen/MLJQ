# -*- coding: utf-8 -*-
"""
Set part-out value — read entirely from BrickStore's local database (catalog blob +
cached price guide). No BrickStore running, no BSX/XML, no scraping.

    python value.py 76342                 # one set: full breakdown
    python value.py 76342 10179 6086      # several sets
    python value.py --all --out report.csv           # every set -> CSV
    python value.py --all --min-year 2020 --out x.csv # only sets from 2020+

    python value.py --all --snapshot          # store today's set values to history DB
    python value.py --snapshot-prices         # store the whole price guide to history DB

The CSV (one row per set, all price metrics + recovery_value + weighted_ratio) is the
raw data for the buy report. --snapshot / --snapshot-prices build the history SQLite.
"""

import sys
import csv
import time
import argparse
import datetime

sys.stdout.reconfigure(encoding="utf-8")

from sortpack import config
from sortpack.catalogdb import CatalogDB
from sortpack.priceguide import PriceGuide, METRIC_ORDER, METRIC_LABELS, parse_key
from sortpack import setvalue
from sortpack.history import HistoryDB

CSV_FIELDS = (["set_id", "name", "year_from", "year_to", "lots", "qty",
               "coverage", "price_date"] + METRIC_ORDER
              + ["recovery_value", "weighted_ratio"])


def _row(v):
    si = v.set_info
    row = {
        "set_id": si.set_id if si else "",
        "name": si.name if si else "",
        "year_from": si.year_from if si else "",
        "year_to": si.year_to if si else "",
        "lots": v.lots,
        "qty": v.total_qty,
        "coverage": round(v.coverage, 4),
        "price_date": datetime.date.fromtimestamp(v.price_mtime).isoformat(),
    }
    for m in METRIC_ORDER:
        row[m] = round(_cad(v.totals[m]), 2)          # CSV values are CAD
    row["recovery_value"] = round(_cad(v.recovery_value), 2)
    row["weighted_ratio"] = round(v.weighted_ratio, 3)
    return row


def print_one(cat, pg, set_no):
    if not cat.has_set(set_no):
        print(f"  {set_no}: introuvable dans le catalogue BrickStore.")
        return
    v = setvalue.set_value(cat, pg, set_no)
    si = v.set_info
    print(f"\n=== {si.set_id} ({si.year_from}) — {si.name} ===")
    print(f"  {v.lots} lots, {v.total_qty} pièces (extras exclus) — "
          f"couverture {v.coverage:.0%} — prix du "
          f"{datetime.date.fromtimestamp(v.price_mtime)}")
    for m in METRIC_ORDER:
        print(f"    {METRIC_LABELS[m]:32} {_cad(v.totals[m]):>12,.2f} $CA")
    print(f"    {'Récupération nette (réalisation)':32} "
          f"{_net_recovery_cad(v):>12,.2f} $CA")
    print(f"    {'Délai estimé de liquidation':32} "
          f"{setvalue.months_to_liquidate(v.weighted_ratio):>9.0f} mois")
    if v.missing_keys:
        print(f"    ⚠ sans prix: {', '.join(v.missing_keys[:8])}")


def _money(x):
    return f"${x:,.2f}"


def _cad(usd):
    """Prices in the DB are USD; everything shown to the user is CAD."""
    return usd * config.USD_TO_CAD


def _pct(a, b):
    return f"{(a/b - 1)*100:+.1f}%" if b else "n/a"


def _net_recovery_cad(v):
    """THE number to compare against: liquidity-weighted part-out on the recovery
    basis (VP-on-sold), in CAD, after the CFB fee."""
    return v.recovery_value * config.USD_TO_CAD * (1 - config.CFB_FEE)


def explain_set(cat, pg, hist, set_no):
    if not cat.has_set(set_no):
        print(f"  {set_no}: introuvable dans le catalogue BrickStore.")
        return
    v = setvalue.set_value(cat, pg, set_no)
    si = v.set_info
    parts = cat.inventory(set_no)                      # extras excluded
    contribs = []
    for p in parts:
        rec = pg.get(p.price_key())
        if not rec:
            continue
        contribs.append((p.qty * rec.current_new_avg, p, rec))
    contribs.sort(reverse=True, key=lambda x: x[0])
    net_cad = _net_recovery_cad(v)
    months = setvalue.months_to_liquidate(v.weighted_ratio)

    print("=" * 78)
    print(f"SET  {si.set_id}   {si.name}   ({si.year_from})")
    print("=" * 78)
    print(f"  {v.lots} lots (extras exclus) | {v.total_qty} pièces | "
          f"couverture prix {v.coverage:.0%} | "
          f"densité {_money(_cad(v.totals['current_new_avg']/v.total_qty))}/pièce")
    print(f"  prix datés du {datetime.date.fromtimestamp(v.price_mtime)}")
    print()
    print("PART-OUT (somme pièces × prix), CAD")
    for m in METRIC_ORDER:
        print(f"    {METRIC_LABELS[m]:34} {_money(_cad(v.totals[m])):>13}")
    print()
    print("RÉCUPÉRATION (CAD)")
    print(f"    Récupération nette          {_money(net_cad):>12}")
    print(f"    Prix d'achat max            {_money(net_cad/config.ROI_FLOOR):>12}")
    print(f"    Ratio de vente              {v.weighted_ratio:>12.2f}")
    print(f"    Délai de liquidation        {months:>7.0f} mois")
    print()
    series = hist.set_value_series(si.set_id)
    if len(series) >= 2:
        print("HISTORIQUE (CAD)")
        for d, avg, recov in series:
            print(f"    {d}   part-out {_money(_cad(avg))}   récup {_money(_cad(recov))}")
        print()
    print("TOP PIÈCES (CAD)")
    for val, p, rec in contribs[:5]:
        print(f"    {p.item_type}{p.item_id:<9} {p.color_name[:16]:16} x{p.qty:<3} "
              f"{_money(_cad(val)):>9}")
    print()


def explain_part(pg, hist, token):
    """token like 'P3001@5', 'P3001@5@B0', or '3001@5' (type defaults to P)."""
    seg = token.split("@")
    typed = seg[0]
    if typed[:1].isalpha():
        item_type, item_id = typed[0].upper(), typed[1:]
    else:
        item_type, item_id = "P", typed
    try:
        color_id = int(seg[1]) if len(seg) > 1 else 0
    except ValueError:
        color_id = 0
    key = f"{item_type}{item_id}@{color_id}@B0"
    rec = pg.get(key)
    print("=" * 78)
    print(f"PIÈCE  {key}")
    print("=" * 78)
    if not rec:
        print("  (aucune donnée de prix pour cette pièce/couleur)")
        return
    r_ = config.USD_TO_CAD
    print("PRIX (CAD)                     min        moy    moy.pond       max")
    print(f"  neuf, actuel        {rec.current_new_min*r_:>10.3f} {rec.current_new_avg*r_:>10.3f}"
          f" {rec.current_new_qtyavg*r_:>10.3f} {rec.current_new_max*r_:>10.3f}")
    print(f"  neuf, 6 mois vendus {rec.sixmonth_new_min*r_:>10.3f} {rec.sixmonth_new_avg*r_:>10.3f}"
          f" {rec.sixmonth_new_qtyavg*r_:>10.3f}")
    print(f"  usagé, actuel       {'':>10} {rec.current_used_avg*r_:>10.3f}")
    print()
    r = rec.ratio_new
    print("OFFRE / DEMANDE")
    print(f"  en vente (neuf)        {rec.current_qty_new:>10,}")
    print(f"  vendus 6 mois (neuf)   {rec.past_six_qty_new:>10,}")
    print(f"  ratio liquidité        {(('%.2f' % r) if r else 'n/a'):>10}   "
          f"(>1 = se vend plus vite qu'il ne se stocke)")
    print(f"  momentum (actuel/6mois): {_pct(rec.current_new_avg, rec.sixmonth_new_avg)}   "
          f"| neuf vs usagé: {_pct(rec.current_new_avg, rec.current_used_avg)}")
    print()
    print("HISTORIQUE DE PRIX (notre SQLite)")
    series = hist.part_price_series(item_type, item_id, color_id)
    if len(series) >= 2:
        for d, avg, qavg in series:
            print(f"    {d}   moy {_money(_cad(avg))}   moy.pond {_money(_cad(qavg))}")
    else:
        print(f"    {len(series)} point — série dans le temps après ≥2 snapshots de prix")
    print()


def run_explain(tokens):
    if not tokens:
        print("usage: python value.py explain <numéro de set | clé de pièce, ex. P3001@5>")
        return
    cat = CatalogDB()
    pg = PriceGuide()
    hist = HistoryDB()
    for tok in tokens:
        tok = tok.strip()
        if "@" in tok:
            explain_part(pg, hist, tok)
        else:
            explain_set(cat, pg, hist, tok)
    hist.close()


def _print_buy_table(rows):
    print(f"      {'set':9} {'src':6} {'qté':>3} {'prix':>6} {'lot$':>7} {'ROI':>5} "
          f"{'%/an':>5} {'$/an×q':>7}  nom")
    for c in rows:
        star = "★ " if c.chosen else "  "
        print(f"    {star}{c.set_id:9} {(c.source or '')[:6]:6} {c.qty:3} {c.price:6.0f} "
              f"{c.lot_cost:7.0f} {c.roi:5.2f} {c.annual_return*100:4.0f}% "
              f"{c.annual_profit_total:7.0f}  {(c.name or '')[:26]}")


def _write_buy_csv(cands, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["chosen", "set_id", "name", "theme", "year", "source", "qty",
                    "lot_cost_cad", "annual_profit_total_cad", "price_cad", "discount",
                    "net_recovery_cad", "roi", "months", "annual_return_pct",
                    "annual_profit_cad", "profit_cad", "max_buy_cad", "coverage", "url"])
        for c in cands:
            w.writerow([1 if c.chosen else 0, c.set_id, c.name, c.theme, c.year,
                        c.source, c.qty, round(c.lot_cost, 2),
                        round(c.annual_profit_total, 2), round(c.price, 2), c.discount,
                        round(c.net_recovery, 2), round(c.roi, 3), round(c.months, 1),
                        round(c.annual_return * 100, 1), round(c.annual_profit, 2),
                        round(c.profit, 2), round(c.max_buy, 2), round(c.coverage, 3),
                        c.url])


def run_job(argv):
    """Full headless pipeline: scrape Amazon → history → rank → DB → optional sheet."""
    ap = argparse.ArgumentParser(prog="value.py job")
    ap.add_argument("--post", action="store_true",
                    help="also POST the buy list to the Google Sheet (if configured)")
    args = ap.parse_args(argv)
    from sortpack import job
    s = job.run(post=args.post, log=print)
    print(f"\n✔ {s['date']} : {s['amazon']} Amazon + {s['costco']} Costco · "
          f"{s['candidates']} candidats · lot {s['chosen']} sets "
          f"(coût {s['batch_cost']:.0f}$, profit/an {s['batch_annual_profit']:.0f}$) → historique"
          + ("  · envoyé au sheet" if s["posted"] else ""))


def run_pricegaps(argv):
    """Emit a .bsx of items missing from BrickStore's price cache (minifigs by default),
    so the gap can be filled in one bulk 'Update Price Guide'."""
    ap = argparse.ArgumentParser(prog="value.py pricegaps")
    ap.add_argument("sets", nargs="*",
                    help="set numbers to scan (default: every set in the latest feed)")
    ap.add_argument("--all-items", action="store_true",
                    help="include parts too, not only minifigs (much larger)")
    ap.add_argument("--out", help="output .bsx path (default: price_gaps.bsx here)")
    args = ap.parse_args(argv)
    from sortpack import pricegaps
    cat = CatalogDB()
    pg = PriceGuide(preload=True)
    set_nos = args.sets
    if not set_nos:
        hist = HistoryDB()
        set_nos = sorted({r[0] for r in hist.latest_feed()})
        hist.close()
        if not set_nos:
            print("Aucun feed en historique — lance d'abord `python value.py job`.")
            return
    path, n = pricegaps.build(cat, pg, set_nos, out_path=args.out,
                              include_parts=args.all_items)
    kind = "articles" if args.all_items else "figurines"
    print(f"\n{n} {kind} sans prix dans le cache, écrits dans :\n  {path}")
    if n:
        print("\nPour combler le trou (une seule fois) :")
        print("  1. Ouvrez ce fichier dans BrickStore.")
        print("  2. Sélectionnez tout (Ctrl+A).")
        print("  3. Menu Édition → « Mettre à jour le guide de prix » (Update Price Guide).")
        print("  4. Laissez terminer (ça télécharge de BrickLink), puis fermez.")
        print("  Ensuite relancez le rapport : les gros sets à figurines se revaluent.")


def run_buy(argv):
    """Ranked buy list from the latest stored Amazon scrape (history DB). All CAD."""
    ap = argparse.ArgumentParser(prog="value.py buy")
    ap.add_argument("--out", help="also export the ranked list to this CSV (optional)")
    ap.add_argument("--top", type=int, default=40, help="rows to print (default 40)")
    args = ap.parse_args(argv)

    from sortpack import buylist
    from sortpack.history import HistoryDB
    t = time.time()
    cat = CatalogDB()
    pg = PriceGuide(preload=True)
    hist = HistoryDB()
    offers = hist.latest_feed()          # Amazon + Costco, cheapest source per set
    print(f"(catalogue + prix chargés en {time.time() - t:.1f}s)")
    if not offers:
        print("Aucune donnée de rabais dans l'historique — lance d'abord "
              "`python value.py job`.")
        return

    exclude = hist.recent_bought_ids(config.REBUY_WINDOW_MONTHS)
    avail, months = hist.available_budget(config.MONTHLY_BUDGET_CAD,
                                          config.BUDGET_ANCHOR_MONTH)
    spent = hist.spent_since(config.BUDGET_ANCHOR_MONTH)
    cands, skipped = buylist.rank(cat, pg, offers, exclude_ids=exclude, budget=avail)
    total_feed = len(cands) + len(skipped)
    chosen = [c for c in cands if c.chosen]

    print(f"\n{len(cands)} sets passent le seuil ROI {config.ROI_FLOOR:g} "
          f"(sur {total_feed} en rabais ; {len(skipped)} écartés).")

    # L'achat remplit un ENVOI, pas un mois : on dit lequel, et ce qu'il lui manque.
    bname, bhave, bneed = buylist.open_batch_target()
    qtys = ", ".join(f"{k} ×{v}" for k, v in
                     sorted((getattr(config, "BUY_QTY_BY_SOURCE", None) or {}).items()))
    if bname:
        print(f"\n★ POUR COMPLÉTER LE BATCH « {bname} » — {bhave:,} pièces déjà dedans, "
              f"{bneed:,} à trouver (plancher {config.BATCH_TARGET_PIECES:,}) — {qtys} :")
    else:
        print(f"\n★ NOUVEL ENVOI — {bneed:,} pièces à réunir — {qtys} :")
    _print_buy_table(chosen)
    tot_cost = sum(c.lot_cost for c in chosen)
    tot_annual = sum(c.annual_profit_total for c in chosen)
    tot_pieces = sum(c.pieces or 0 for c in chosen)
    if tot_cost:
        print(f"      → {len(chosen)} sets | {tot_pieces:,} pièces | coût {_money(tot_cost)} | "
              f"profit/an {_money(tot_annual)} | "
              f"RENDEMENT {tot_annual / tot_cost * 100:.0f}%/an sur capital")
        # Le plancher de pieces prime sur le budget : on le dit au lieu de le taire.
        if tot_pieces < bneed:
            print(f"      ⚠ il manque {bneed - tot_pieces:,} pièces : les candidats au-dessus "
                  f"du seuil ROI {config.ROI_FLOOR:g} n'y suffisent pas. On n'achète pas "
                  f"moins bon pour remplir — l'envoi attendra le prochain rabais.")
        else:
            over = tot_pieces - bneed
            msg = (f"      ✔ l'envoi est complet : {bhave + tot_pieces:,} pièces "
                   f"(plancher {config.BATCH_TARGET_PIECES:,})")
            if over > 0.15 * bneed:
                # On prend les sets les mieux classés, pas ceux qui tombent juste : le dernier
                # peut donc déborder largement. Ce n'est pas perdu — le surplus ouvre l'envoi
                # suivant — mais c'est du capital immobilisé plus tôt, alors on le dit.
                msg += (f"\n      ⚠ {over:,} pièces de plus que nécessaire (+{over/bneed:.0%}) : "
                        f"le dernier set choisi déborde. Le surplus ouvrira l'envoi suivant.")
            print(msg)
        if tot_cost > avail:
            print(f"      ⚠ budget dépassé de {_money(tot_cost - avail)} "
                  f"(dispo {avail:,.0f}$, cumul {months} mois depuis "
                  f"{config.BUDGET_ANCHOR_MONTH}, déjà dépensé {spent:,.0f}$) — assumé : "
                  f"un envoi incomplet immobilise le tri, un dépassement ne fait qu'avancer "
                  f"une dépense du mois suivant.")
        else:
            print(f"      budget : {_money(tot_cost)} sur {avail:,.0f}$ disponibles")

    print(f"\nTOP {args.top} CANDIDATS (★ = lot du mois) :")
    _print_buy_table(cands[:args.top])

    if args.out:
        _write_buy_csv(cands, args.out)
        print(f"\nliste complète ({len(cands)} candidats) → {args.out}")


def report_all(cat, pg, out_path, min_year=None):
    t = time.time()
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for set_no in cat.all_set_numbers():
            si = cat.set_info(set_no)
            if min_year and (not si.year_from or si.year_from < min_year):
                continue
            v = setvalue.set_value(cat, pg, set_no)
            w.writerow(_row(v))
            n += 1
    print(f"{n:,} sets -> {out_path}  ({time.time() - t:.1f}s)")


def main():
    # `value.py explain <set|part> …` — rich single-item breakdown
    argv = sys.argv[1:]
    if argv and argv[0] == "explain":
        return run_explain(argv[1:])
    if argv and argv[0] == "buy":
        return run_buy(argv[1:])
    if argv and argv[0] == "job":
        return run_job(argv[1:])
    if argv and argv[0] == "pricegaps":
        return run_pricegaps(argv[1:])

    ap = argparse.ArgumentParser()
    ap.add_argument("sets", nargs="*", help="set numbers, e.g. 76342")
    ap.add_argument("--all", action="store_true", help="value every set in the catalog")
    ap.add_argument("--min-year", type=int, help="with --all: only sets from this year on")
    ap.add_argument("--out", default="set_values.csv", help="CSV path for --all")
    ap.add_argument("--snapshot", action="store_true",
                    help="store set values into the history SQLite (use with --all "
                         "or a set list) instead of / in addition to printing")
    ap.add_argument("--snapshot-prices", action="store_true",
                    help="store the whole price guide into the history SQLite")
    args = ap.parse_args()

    bulk = args.all or args.snapshot_prices
    t = time.time()
    cat = CatalogDB()
    # preload the whole price guide for bulk work (avoids per-part sqlite hits)
    pg = PriceGuide(preload=bulk)
    print(f"(catalogue + prix chargés en {time.time() - t:.1f}s)")

    if args.snapshot_prices:
        hist = HistoryDB()
        n, pdate = hist.snapshot_prices(pg, progress=lambda k: print(f"  …{k:,} pièces"))
        if n:
            print(f"{n:,} prix de pièces enregistrés pour la date {pdate}.")
        else:
            print(f"Les prix du {pdate} sont déjà dans l'historique (rien à faire).")
        hist.close()

    # --all writes the CSV report, unless we're snapshotting (then the DB is the output)
    if args.all and not args.snapshot:
        report_all(cat, pg, args.out, args.min_year)

    if args.snapshot and (args.all or args.sets):
        hist = HistoryDB()
        today = datetime.date.today().isoformat()
        if args.all:
            n = hist.snapshot_all_sets(cat, pg, min_year=args.min_year,
                                       progress=lambda k: print(f"  …{k:,} sets"))
            print(f"{n:,} valeurs de sets enregistrées pour le {today}.")
        else:
            for s in args.sets:
                if cat.has_set(s.strip()):
                    hist.snapshot_set(setvalue.set_value(cat, pg, s.strip()), today)
            print(f"Valeurs enregistrées pour le {today}.")
        hist.close()

    if args.sets and not args.snapshot:
        for s in args.sets:
            print_one(cat, pg, s.strip())

    if not (args.sets or args.all or args.snapshot_prices):
        ap.print_help()


if __name__ == "__main__":
    main()
