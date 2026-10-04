# -*- coding: utf-8 -*-
"""
Weekly CFB/UFB SALES → MLJQ Journal, mirroring boxoffice-quebec's streamlined weekly flow
(netlify/lib/weekly.js + scripts/cfb-streamlined-sheet.gs). Reuses the cookie/session from
cfb_inventory. For a business week (Mon→Fri, ending Friday) it pools each portal's sheetable
sales and posts, per source, two Journal rows via the Apps Script 'salesweek' action:
    "Ventes - CA/US"    catégorie V,  Montant = gross   (CA tax-included; US zero-rated)
    "Frais CFB - CA/US" catégorie FT, Montant = -fees   (fees = gross - payout)
J/K (TPS/TVQ) and L (CMV) are filled by the sheet's own formulas — we never write them.

Sales report: /bricklink/inventory_vendor_reports (date-range form). Same vendor selector
as the inventory report (config.CFB_VENDOR; MLJQ once it exists, "Bino" to test).

STATUS: fetch + aggregation built; the row PARSER is a faithful port pending a real sales
report to confirm the money format per portal — run `--dump-sales` first.
"""

import os
import re
import sys
import html
import datetime

from . import config
from . import fx
from .cfb_inventory import (SOURCES, _session, _fetch, _FormParser, vendor_options,
                            match_vendor, match_vendor_source, CfbAuthError)

SALES_NEW_PATH = "/bricklink/inventory_vendor_reports/new"
SALES_POST_PATH = "/bricklink/inventory_vendor_reports"

_TAX_FACTOR = 1.0 + config.PURCHASE_TAX_RATE          # GST+QST, CA sales are tax-included


# --- week helpers ------------------------------------------------------------
def _shift(ymd, n):
    d = datetime.date.fromisoformat(ymd) + datetime.timedelta(days=n)
    return d.isoformat()


def friday_of_week(ymd):
    """The Friday ending the Mon→Fri business week containing `ymd`."""
    dow = datetime.date.fromisoformat(ymd).weekday()   # Mon=0 … Sun=6
    delta = {5: -1, 6: -2}.get(dow, 4 - dow)           # Sat→Fri-1, Sun→Fri-2, else this Fri
    return _shift(ymd, delta)


def weekdays_ending_friday(friday):
    return [_shift(friday, n) for n in (-4, -3, -2, -1, 0)]   # Mon..Fri


# --- fetch -------------------------------------------------------------------
_START_TOKENS = ("from", "start", "begin", "start_date", "date_from", "debut", "début")
_END_TOKENS = ("to", "end", "until", "end_date", "date_to", "fin")


def _bracket_token(name):
    """The last [bracket] token of a field name ('…[from]' → 'from'), else the name."""
    m = re.search(r"\[([^\]]+)\]\s*$", name or "")
    return (m.group(1) if m else (name or "")).lower()


def _date_fields(fields):
    """Identify the start/end date field NAMES by exact bracket token (so 'type', 'vendor_id'
    etc. are never mistaken for a date field — that clobbered [type] and emptied the report)."""
    start = next((n for n in fields if _bracket_token(n) in _START_TOKENS), None)
    end = next((n for n in fields if _bracket_token(n) in _END_TOKENS), None)
    return start, end


def load_sales_form(source, opener):
    text, _ = _fetch(opener, source, SALES_NEW_PATH)
    p = _FormParser()
    p.feed(text)
    csrf = p.csrf or p.meta_csrf
    if not csrf:
        raise RuntimeError(f"CFB[{source}] : jeton CSRF introuvable sur le rapport de ventes")
    return csrf, (p.action or SALES_POST_PATH), p.fields, text


def generate_sales_report(source, start_date, end_date, vendor=None, opener=None):
    """Return the sales report HTML for [start_date, end_date] (yyyy-mm-dd) for `vendor`."""
    opener = opener or _session(source)
    csrf, action, fields, form_html = load_sales_form(source, opener)
    fields = dict(fields)
    fields["_csrf_token"] = csrf
    sname, ename = _date_fields(fields)
    if sname:
        fields[sname] = start_date
    if ename:
        fields[ename] = end_date
    if vendor:
        opts = vendor_options(form_html)
        match = match_vendor_source(vendor, source, opts)
        if match is None:
            raise RuntimeError(f"CFB[{source}] : vendeur « {vendor} » introuvable — "
                               f"disponibles : {[l for _, l in opts] or '(aucun)'}")
        vfield = next((k for k in fields if "vendor_id" in k), None)
        if vfield:
            fields[vfield] = match
    text, _ = _fetch(opener, source, action, data=fields)
    return text


# --- parse (faithful port of boxoffice parseReportRows) ----------------------
def _strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def _fr_money(text):
    """'1 234,56 $CA' / '5,64 $CA' → 1234.56 (French/CAD formatting)."""
    if not text:
        return None
    s = re.sub(r"\$CA|CAD|\$", "", _strip(text)).strip()
    m = re.search(r"-?\d{1,3}(?:[   ]\d{3})*(?:,\d+)?|-?\d+(?:,\d+)?", s)
    if not m:
        return None
    v = re.sub(r"[   ]", "", m.group(0)).replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _int_cell(text):
    m = re.search(r"-?\d+", _strip(text) or "")
    return int(m.group(0)) if m else None


def parse_sales_rows(report_html, date_filter=None):
    """Rows from every report table, tagged by their section (preceding <h5>). Columns:
    Date|Source|Type|ItemNo|Color|Cond|Qty|UnitPriceUSD|Total|Payout. `date_filter` is a
    yyyy-mm-dd string (exact match) or None. Returns list of row dicts."""
    rows = []
    section = "Unknown"
    for m in re.finditer(r"<h5[^>]*>(.*?)</h5>|<table[^>]*class=\"[^\"]*table[^\"]*\"[^>]*>(.*?)</table>",
                         report_html, re.S | re.I):
        if m.group(1) is not None:
            section = _strip(m.group(1)) or section
            continue
        body = re.search(r"<tbody[^>]*>(.*?)</tbody>", m.group(2), re.S | re.I)
        scope = body.group(1) if body else m.group(2)
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", scope, re.S | re.I):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
            if len(cells) < 10:
                continue
            date = _strip(cells[0])
            qty = _int_cell(cells[6])
            item_id = _strip(cells[3])
            if not date or not item_id or qty is None:
                continue
            if date_filter and date != date_filter:
                continue
            rows.append({
                "section": section, "date": date, "source": _strip(cells[1]),
                "type": _strip(cells[2]), "itemId": item_id, "color": _strip(cells[4]),
                "cond": _strip(cells[5]), "qty": qty,
                "total": _fr_money(cells[8]) or 0.0, "payout": _fr_money(cells[9]) or 0.0,
            })
    return rows


def is_sheetable_sale(row):
    """Only 'Platforms' rows (BrickLink/BrickOwl/etc.) are real sales. Unlike boxoffice, we
    EXCLUDE Manual Outputs: in the MLJQ/Bino data every one is a 'Manual Withdrawal
    (removed/missing/substitution/…)' — an inventory correction/loss, not a cash sale — so
    counting them (even with payout>0) would book phantom revenue. Pick Lists / Inventory
    Syncs are never sales."""
    return row["section"] == "Platforms"


def aggregate_rows(rows):
    parts = total = payout = 0.0
    lots = set()
    for r in rows:
        parts += r["qty"]
        total += r["total"]
        payout += r["payout"]
        lots.add(f"{r['itemId']}|{r['color']}|{r['cond']}")
    return {"parts": int(parts), "lots": len(lots),
            "total": round(total, 2), "payout": round(payout, 2),
            "fees": round(total - payout, 2)}


# --- weekly roll-up ----------------------------------------------------------
def run_week(friday, vendor=None, sources=("CA", "US"), log=print):
    """Pool each source's sheetable sales for the Mon→Fri week ending `friday`. US totals are
    converted to CAD. Returns (per, auth_failed): per = {source: {…, post_total, post_fees}}
    for sources that had sales; auth_failed = set of sources whose cookie expired (their week
    is left unposted so it retries after you refresh cookies)."""
    if vendor is None:
        vendor = config.CFB_VENDOR
    days = weekdays_ending_friday(friday)
    per, auth_failed = {}, set()
    for s in sources:
        try:
            opener = _session(s)
            pooled = []
            for d in days:
                html_text = generate_sales_report(s, d, d, vendor=vendor, opener=opener)
                pooled.extend([r for r in parse_sales_rows(html_text, d)
                               if is_sheetable_sale(r)])
        except CfbAuthError as e:
            log(f"  {s} : cookie expiré — {e} (semaine laissée en attente).")
            auth_failed.add(s)
            continue
        except Exception as e:                        # vendor-not-found, transient, etc.
            log(f"  {s} : erreur — {str(e)[:110]} (semaine ignorée, non postée).")
            continue
        if not pooled:
            log(f"  {s} : aucune vente pour la semaine du {friday}.")
            continue
        rate = None
        if s == "US":                                 # convert the whole week at the BoC rate
            rate, rate_date, src = fx.get_usd_cad_rate(friday)   # for the week-ending date
            for r in pooled:
                r["total"] *= rate
                r["payout"] *= rate
        agg = aggregate_rows(pooled)
        taxable = (s == "CA")
        agg["taxable"] = taxable
        agg["post_total"] = round(agg["total"] * _TAX_FACTOR, 2) if taxable else agg["total"]
        agg["post_fees"] = round(agg["fees"] * _TAX_FACTOR, 2) if taxable else agg["fees"]
        if rate is not None:
            agg["fx_rate"], agg["fx_date"], agg["fx_source"] = rate, rate_date, src
        per[s] = agg
        log(f"  {s} : brut {agg['post_total']:,.2f}$ · frais {agg['post_fees']:,.2f}$ · "
            f"{agg['parts']} pièces / {agg['lots']} lots"
            + (f" (fx {rate:.4f} {src} {rate_date})" if rate is not None else "") + ".")
    return per, auth_failed


def _most_recent_complete_friday(today=None):
    """The last week-ending Friday that has fully passed (a complete Mon→Fri week)."""
    today = today or datetime.date.today()
    f = friday_of_week(today.isoformat())
    if datetime.date.fromisoformat(f) >= today:      # this week's Friday hasn't passed yet
        f = _shift(f, -7)
    return f


def catch_up(vendor=None, sources=("CA", "US"), anchor=None, commit=False, log=print):
    """Post EVERY complete week from the anchor (config.CFB_SALES_ANCHOR) to the most recent
    finished week that isn't already posted — so a laptop closed for weeks backfills all the
    missed weeks with correct per-week date ranges. Idempotent (won't double-post). On an
    expired cookie it emails once and leaves those weeks pending for the next run. Dry-run
    unless commit=True."""
    from .history import HistoryDB
    from . import sheet
    want = vendor or config.CFB_VENDOR
    # Pre-check the target vendor exists on the portal (e.g. MLJQ isn't created yet) so a
    # daily task exits cleanly instead of error-looping every week. Cookie expiry → alert.
    try:
        opts = vendor_options(load_sales_form("CA", _session("CA"))[3])
        if match_vendor_source(want, "CA", opts) is None:
            log(f"Vendeur « {want} » absent du portail ({[l for _, l in opts]}) — "
                f"rien à faire (crée le vendeur, ou passe --vendor).")
            return {"note": f"vendeur {want} inexistant", "posted": [], "auth_failed": []}
    except CfbAuthError as e:
        if commit:
            try:
                sheet.send_alert("Cookie CFB expiré (ventes)",
                                 f"Rattrapage des ventes impossible — cookie expiré ({e}). "
                                 f"Mets-le à jour via « Nouveaux cookies ».")
            except Exception:
                pass
        log(f"Cookie CFB expiré — {e}")
        return {"note": "cookie expiré", "posted": [], "auth_failed": ["CA"]}
    except Exception as e:
        log(f"Pré-vérification vendeur échouée ({str(e)[:90]}) — on continue quand même.")

    last_fri = _most_recent_complete_friday()
    anchor_fri = friday_of_week(anchor) if anchor else (config.CFB_SALES_ANCHOR or last_fri)
    h = HistoryDB()
    posted, auth_failed, post_failed = [], set(), set()
    try:
        f = anchor_fri
        while f <= last_fri:
            pending = [s for s in sources if not h.get_meta(f"cfb_sales_{f}_{s}")]
            if pending:
                log(f"Semaine {f} : {pending}")
                per, af = run_week(f, vendor=vendor, sources=tuple(pending), log=log)
                auth_failed |= af
                for s, agg in per.items():
                    if not commit:
                        log(f"  {s} : (dry-run) Ventes {agg['post_total']:,.2f}$ + "
                            f"Frais {agg['post_fees']:,.2f}$.")
                        continue
                    try:
                        reply = sheet.post_sales_week(f, s, agg["post_total"],
                                                      agg["post_fees"], agg["lots"],
                                                      agg["parts"])
                    except Exception as e:                # a flaky POST must not crash the run
                        log(f"  {s} : POST échoué — {str(e)[:90]} (laissé en attente).")
                        post_failed.add(s)
                        continue
                    if "ventesRow" not in reply:          # only mark done on a confirmed write
                        log(f"  {s} : réponse inattendue → {reply[:120]} (non marqué).")
                        post_failed.add(s)
                        continue
                    h.set_meta(f"cfb_sales_{f}_{s}",
                               datetime.datetime.now().isoformat(timespec="seconds"))
                    posted.append((f, s))
                    log(f"  {s} : posté → {reply}")
            f = _shift(f, 7)
    finally:
        h.close()
    if auth_failed and commit:
        try:
            sheet.send_alert(
                f"Cookie CFB expiré (ventes {', '.join(sorted(auth_failed))})",
                f"Des ventes n'ont pas été postées au Journal — cookie(s) "
                f"{', '.join(sorted(auth_failed))} expiré(s). Mets-les à jour via "
                f"« Nouveaux cookies » puis relance : le rattrapage postera les semaines "
                f"en attente.")
        except Exception as e:
            log(f"  ⚠ alerte non envoyée : {str(e)[:80]}")
    return {"anchor": anchor_fri, "through": last_fri, "posted": posted,
            "auth_failed": sorted(auth_failed), "post_failed": sorted(post_failed)}


def post_week(friday, vendor=None, sources=("CA", "US"), commit=False, log=print):
    """Roll up the week and (if commit) post each source's V+FT rows to the Journal, skipping
    a (week, source) already posted (idempotent). Dry-run by default."""
    from .history import HistoryDB
    from . import sheet
    per, _auth = run_week(friday, vendor=vendor, sources=sources, log=log)
    if not per:
        return {"week": friday, "posted": [], "note": "aucune vente"}
    h = HistoryDB()
    posted = []
    try:
        for s, agg in per.items():
            key = f"cfb_sales_{friday}_{s}"
            if h.get_meta(key):
                log(f"  {s} : déjà posté pour {friday} — ignoré.")
                continue
            if not commit:
                log(f"  {s} : (dry-run) posterait Ventes {agg['post_total']:,.2f}$ + "
                    f"Frais {agg['post_fees']:,.2f}$.")
                continue
            reply = sheet.post_sales_week(friday, s, agg["post_total"], agg["post_fees"],
                                          agg["lots"], agg["parts"])
            h.set_meta(key, datetime.datetime.now().isoformat(timespec="seconds"))
            posted.append(s)
            log(f"  {s} : posté → {reply}")
    finally:
        h.close()
    return {"week": friday, "posted": posted}


def reset_posted(log=print):
    """Clear the 'already-posted' week markers so the next catch-up re-posts (for testing).
    Does NOT touch the sheet — delete the test rows in the Journal by hand."""
    from .history import HistoryDB
    h = HistoryDB()
    try:
        cur = h._conn.execute("DELETE FROM meta WHERE key LIKE 'cfb_sales\\_%' ESCAPE '\\'")
        h._conn.commit()
        n = cur.rowcount
    finally:
        h.close()
    log(f"{n} marqueur(s) de semaine effacé(s) — le prochain rattrapage re-postera "
        f"(supprime les lignes de test dans le Journal à la main).")
    return n


# --- CLI ---------------------------------------------------------------------
def _dump_sales(out_dir, vendor=None):
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=30)).isoformat()   # a past range with real sales
    end = today.isoformat()
    print(f"[dump v2] plage {start} → {end}")
    for s in SOURCES:
        try:
            opener = _session(s)
            csrf, action, fields, form = load_sales_form(s, opener)
            sname, ename = _date_fields(fields)
            with open(os.path.join(out_dir, f"cfb_sales_new_{s}.html"), "w", encoding="utf-8") as f:
                f.write(form)
            print(f"[{s}] /new sauvé — CSRF ok, action={action}, dates=({sname},{ename}), "
                  f"champs={list(fields)[:8]}")
            rep = generate_sales_report(s, start, end, vendor=vendor, opener=opener)
            with open(os.path.join(out_dir, f"cfb_sales_report_{s}.html"), "w", encoding="utf-8") as f:
                f.write(rep)
            rows = parse_sales_rows(rep)                          # no date filter for the dump
            sheetable = [r for r in rows if is_sheetable_sale(r)]
            print(f"[{s}] rapport {start}→{end} : {len(rows)} lignes, "
                  f"{len(sheetable)} ventes → {aggregate_rows(sheetable)}")
        except Exception as e:
            print(f"[{s}] ERREUR: {type(e).__name__}: {str(e)[:200]}")
    print(f"\nFichiers dans : {out_dir}\nPartage cfb_sales_report_CA.html (haut + une ligne) "
          f"pour confirmer le format des montants.")


if __name__ == "__main__":
    try:                              # accented/→ log lines must not crash a cp1252 console
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    _vendor = None
    if "--vendor" in sys.argv:
        _i = sys.argv.index("--vendor")
        _vendor = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None
    if "--dump-sales" in sys.argv:
        _dump_sales(os.path.join(os.getcwd(), "_cfb_debug"), vendor=_vendor)
    elif "--reset" in sys.argv:
        reset_posted()
    elif "--catchup" in sys.argv:
        commit = "--commit" in sys.argv
        print(f"Rattrapage des ventes ({'RÉEL' if commit else 'dry-run'}, "
              f"vendeur {_vendor or config.CFB_VENDOR}) :")
        print(catch_up(vendor=_vendor, commit=commit))
    elif "--week" in sys.argv:
        _i = sys.argv.index("--week")
        _arg = sys.argv[_i + 1] if _i + 1 < len(sys.argv) and not sys.argv[_i + 1].startswith("-") else None
        friday = friday_of_week(_arg or datetime.date.today().isoformat())
        commit = "--commit" in sys.argv
        print(f"Semaine se terminant le {friday} (vendeur {_vendor or config.CFB_VENDOR}, "
              f"{'RÉEL' if commit else 'dry-run'}) :")
        print(post_week(friday, vendor=_vendor, commit=commit))
    else:
        print("usage: python -m sortpack.cfb_sales [--dump-sales | --week [yyyy-mm-dd] "
              "[--commit]] [--vendor NAME]")
