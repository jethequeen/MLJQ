# -*- coding: utf-8 -*-
"""
The phone's Finances / Achats snapshot (``dashboard.json`` in the CFB Drive folder).

Same contract as ``sort_index.json``: the PC writes it, the phone only reads it, so the
mobile web-app never has to reach the history DB, the catalog or the Google Sheet. It
carries what the two new tabs show:

  * **Achats** — the latest ranked buy list (the monthly batch first, then the runners-up),
    with each offer's store, price, ROI, months-to-liquidate and buy link.
  * **Finances** — the "reste à investir" (cumulative available budget − spent), the
    per-month budgets, the CFB inventory values per seller, the cookie status and how
    stale the BrickStore catalog / price guide are.

Rewritten after every processed phone command (``sortpack.remote``) and, throttled to
``config.MOBILE_DASHBOARD_REFRESH_SECONDS``, by the desktop poll. Every write is
best-effort: a Drive hiccup must never break the desktop or the queue.
"""

import os
import json
import datetime

from . import config
from .history import HistoryDB, BUY_SHEET_COLS

# meta keys: 'mljq_last_<action>' — when each phone-triggered job last succeeded, so the
# Finances tab can show "Synchronisé il y a 2 h" instead of an unlabelled button.
LAST_RUN_META = "mljq_last_%s"


def mark_run(action, hist=None, when=None):
    """Record that `action` just completed (Finances tab shows it under the button)."""
    h = hist or HistoryDB()
    try:
        h.set_meta(LAST_RUN_META % action,
                   when or datetime.datetime.now().isoformat(timespec="seconds"))
    finally:
        if hist is None:
            h.close()


def _age_days(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return (datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)).days


def _num(v):
    """JSON-safe number (sqlite gives Decimal-free floats, but None/str sneak in)."""
    if v is None or v == "":
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """Feed discounts are STRINGS ("44%", "" when full price) — the phone wants a number.
    Returns the percentage (44.0) or None when there is no discount."""
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace(",", ".")
    if not s:
        return None
    try:
        n = round(float(s), 2)
    except ValueError:
        return None
    return abs(n) or None


def _journal_counts(cat, set_id):
    """(lots, pièces PAR COPIE) pour les colonnes H/I du Journal — la même règle que le
    bouton « Acheter un set » du bureau, vérifiée contre les maîtres réellement expédiés
    (43011, 42680 et 77256 tombent au lot près et à la pièce près) :

      * les LOTS ne se multiplient PAS par la quantité : 9 exemplaires du même set, ce sont
        les mêmes lots (pièce + couleur) avec 9× la quantité dedans. Et ils se comptent SANS
        les pièces en trop, parce qu'une pièce en trop double presque toujours une pièce déjà
        au set : elle grossit un lot existant, elle n'en crée pas ;
      * les PIÈCES se multiplient, elles, et comptent les extras — la boîte les contient et
        CFB les reçoit.

    D'où le retour dissymétrique : les lots sont définitifs, les pièces sont par copie et la
    page les multiplie par la quantité réellement saisie."""
    if cat is None or not set_id:
        return None, None
    num = str(set_id).split("-")[0]
    try:
        return (len(cat.inventory(num, include_extras=False)),
                sum(p.qty for p in cat.inventory(num, include_extras=True)))
    except Exception:
        return None, None


def _buy_row(row, cat=None):
    """One buy_list row (BUY_SHEET_COLS order) -> the dict the phone renders."""
    d = dict(zip(BUY_SHEET_COLS, row))
    out = {"set_id": str(d.get("set_id") or ""),
           "name": d.get("name") or "",
           "source": (d.get("source") or "").lower(),
           "url": d.get("url") or "",
           "year": d.get("year") or ""}
    out["number"] = out["set_id"].split("-")[0]
    for k in ("rank", "qty", "pieces"):
        try:
            out[k] = int(d.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    for k in ("price_cad", "lot_cost_cad", "roi", "annual_return_pct",
              "annual_profit_cad", "annual_profit_total_cad", "months"):
        out[k] = _num(d.get(k))
    out["discount"] = _pct(d.get("discount"))
    # Pour « J'ai acheté » : le téléphone n'a pas le catalogue, donc le PC lui donne les
    # comptes PAR COPIE et la page les multiplie par la quantité réellement achetée.
    lots1, pcs1 = _journal_counts(cat, out["number"])
    out["journal_lots"], out["journal_pieces"] = lots1, pcs1
    # Pre-discount price, so the card can strike it through like the "Lot du mois" email.
    price, disc = out["price_cad"], out["discount"]
    out["price_before"] = (round(price / (1 - disc / 100.0), 2)
                           if price and disc and disc < 100 else None)
    return out


def _budget(h):
    avail, months = h.available_budget(config.MONTHLY_BUDGET_CAD, config.BUDGET_ANCHOR_MONTH)
    spent = h.spent_since(config.BUDGET_ANCHOR_MONTH)
    budgets = h.monthly_budgets(config.BUDGET_ANCHOR_MONTH)
    return {
        "available": _num(avail),
        "spent": _num(spent),
        "granted": _num(sum(a for _, a in budgets)) if budgets else None,
        "anchor": config.BUDGET_ANCHOR_MONTH,
        "months_elapsed": months,
        "fallback_monthly": _num(config.MONTHLY_BUDGET_CAD),
        "dynamic": bool(budgets),
        "months": [{"month": m, "amount": _num(a)} for m, a in budgets],
    }


def _buy_list(h):
    run_date = h.latest_buy_list_date()
    if not run_date:
        return {"run_date": None, "batch": [], "others": [],
                "batch_cost": None, "batch_annual_profit": None}
    chosen_ids = {str(r[BUY_SHEET_COLS.index("set_id")]) for r in
                  h.latest_buy_list(chosen_only=True)}
    try:
        from . import catalogdb
        cat = catalogdb.shared()
    except Exception:
        cat = None                 # sans catalogue : pas de comptes, le reste marche quand même
    batch, others = [], []
    for row in h.latest_buy_list():
        r = _buy_row(row, cat=cat)
        (batch if r["set_id"] in chosen_ids else others).append(r)
    extra = int(getattr(config, "MOBILE_DASHBOARD_EXTRA_ROWS", 0) or 0)
    # L'achat remplit un ENVOI, pas un mois : le téléphone doit dire lequel et où il en est,
    # sinon un lot qui dépasse le budget paraît absurde alors qu'il est voulu.
    try:
        from . import buylist
        bname, bhave, bneed = buylist.open_batch_target()
    except Exception:
        bname, bhave, bneed = None, 0, int(getattr(config, "BATCH_TARGET_PIECES", 20000))
    # « Déjà dans l'envoi » mélange deux choses très différentes : ce qui est sur la table et
    # ce qui est acheté mais pas encore arrivé. Le téléphone doit pouvoir le dire, sinon on
    # cherche des boîtes qui ne sont pas encore livrées.
    bhere, bpending, bfull = bhave, [], False
    try:
        from . import batch as _batch
        if not bname:
            # open_batch_target ignore les batchs COMPLETS — c'est ce qu'il faut pour choisir
            # quoi acheter, mais le téléphone, lui, doit voir qu'un envoi est prêt à fermer.
            # Sinon il affiche « Nouvel envoi » et on ne sait pas que le précédent attend.
            for nm in sorted(_batch.all_batches()):
                s2 = _batch.status(nm)
                if not s2["closed"] and s2["full"]:
                    bname, bhave, bneed = nm, s2["pieces"], 0
                    break
        if bname:
            st = _batch.status(bname)
            bhere = st["pieces_here"]
            bpending = [{"set": r["set"], "pieces": r["expected"]}
                        for r in st["sets"] if r["expected"]]
            bfull = bool(st["full"])
    except Exception:
        pass
    got = sum(r.get("pieces") or 0 for r in batch)
    return {
        "run_date": run_date,
        "batch": batch,
        "others": others[:extra] if extra else others,   # 0 = all (the phone filters/sorts)
        "others_total": len(others),
        "batch_cost": _num(sum(r["lot_cost_cad"] or 0 for r in batch)),
        "batch_annual_profit": _num(sum(r["annual_profit_total_cad"] or 0 for r in batch)),
        "batch_pieces": got,
        "shipment": {
            "name": bname,                 # le batch à compléter, ou None pour un envoi neuf
            "have": bhave,                 # pièces déjà dedans (physiques + achetées)
            "here": bhere,                 # ... dont physiquement là
            "pending": bpending,           # ... et achetées, pas encore reçues
            "full": bfull,                 # l'envoi est complet : il peut être fermé
            "need": bneed,                 # pièces qu'il reste à acheter
            "target": int(getattr(config, "BATCH_TARGET_PIECES", 20000)),
            "got": got,                    # pièces du lot proposé
            "complete": got >= bneed,
            "over": max(0, got - bneed),   # ce qu'on achète en trop (ouvrira l'envoi suivant)
            "qty_by_source": dict(getattr(config, "BUY_QTY_BY_SOURCE", None) or {}),
        },
    }


def _inventory(h):
    out = []
    for cfg in getattr(config, "CFB_INVENTORY_VENDORS", []):
        vendor = cfg.get("vendor")
        row = h.latest_inventory_value(vendor=vendor)
        if not row:
            continue
        out.append({"vendor": vendor, "as_of": row.get("as_of"),
                    "ca": _num(row.get("ca_value")), "us": _num(row.get("us_value")),
                    "total": _num(row.get("total_value")),
                    "pieces": row.get("total_pieces"), "lots": row.get("total_lots")})
    return out


def _cookies(h):
    out = []
    for src in ("CA", "US"):
        out.append({"source": src,
                    "status": h.get_meta("cfb_status_%s" % src) or "unknown",
                    "checked": h.get_meta("cfb_checked_%s" % src) or ""})
    return out


def _jobs(h):
    """Last successful run of each phone-triggerable job (ISO strings, '' when never)."""
    actions = ("sync_purchases", "buy_report", "cfb_inventory", "end_of_month",
               "check_cookies")
    return {a: (h.get_meta(LAST_RUN_META % a) or "") for a in actions}


def _delivered(h, limit=None):
    """Les sets LIVRÉS, pour l'onglet Historique du téléphone.

    Il lit la même source que le GUI — la table `sent` — parce qu'au « Livré » le dossier du
    set part à la corbeille : il n'y a plus rien à balayer, et `sort_index.json` ne liste que
    ce qui existe encore. Le nom vient du catalogue et l'envoi du registre, comme au bureau.
    """
    rows = h.delivered(limit=limit)
    if not rows:
        return []
    try:
        from . import catalogdb
        cat = catalogdb.shared()
    except Exception:
        cat = None
    try:
        from . import batch as _batch
        bmap = {}
        for nm, b in _batch.all_batches().items():
            for s in b.get("sets", []):
                bmap[str(s)] = str(nm)
    except Exception:
        bmap = {}
    out = []
    for r in rows:
        num = str(r["set_id"]).split("-")[0]
        name = ""
        if cat is not None:
            try:
                info = cat.set_info(num)
                name = info.name if info else ""
            except Exception:
                name = ""
        out.append({"set_id": r["set_id"], "number": num, "name": name,
                    "sent_date": r["sent_date"], "qty": r["qty"],
                    "batch": bmap.get(num, "")})
    return out


def build_dashboard(hist=None):
    """The full payload the phone reads. Opens its own HistoryDB unless one is passed."""
    h = hist or HistoryDB()
    try:
        buy = _buy_list(h)
        payload = {
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "budget": _budget(h),
            "buy_list": buy,
            "delivered": _delivered(h, limit=getattr(config, "MOBILE_HISTORY_ROWS", 0) or None),
            "inventory": _inventory(h),
            "cookies": _cookies(h),
            "jobs": _jobs(h),
            "freshness": {
                "catalog_days": _age_days(config.BRICKSTORE_DB),
                "priceguide_days": _age_days(config.BRICKSTORE_PRICEGUIDE),
                "catalog_stale_days": getattr(config, "CATALOG_STALE_DAYS", None),
                "priceguide_stale_days": getattr(config, "PRICEGUIDE_STALE_DAYS", None),
                "buy_list_date": buy["run_date"],
            },
        }
    finally:
        if hist is None:
            h.close()
    return payload


def write_dashboard(hist=None, path=None):
    """Write the snapshot. Best-effort: returns the path on success, None on any failure
    (a Drive hiccup or a locked DB must never break the caller)."""
    path = path or config.MOBILE_DASHBOARD_FILE
    try:
        payload = build_dashboard(hist)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path
    except Exception:
        return None


def maybe_write_dashboard(hist=None, path=None, max_age_seconds=None):
    """Write it only if the file is missing or older than the refresh interval — the
    desktop poll runs every REMOTE_POLL_SECONDS and this reads the whole buy list."""
    path = path or config.MOBILE_DASHBOARD_FILE
    if max_age_seconds is None:
        max_age_seconds = getattr(config, "MOBILE_DASHBOARD_REFRESH_SECONDS", 600)
    try:
        age = (datetime.datetime.now().timestamp() - os.path.getmtime(path))
        if age < max_age_seconds:
            return None
    except OSError:
        pass                                   # missing → write it
    return write_dashboard(hist, path)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Write the phone's dashboard.json snapshot.")
    ap.add_argument("--print", action="store_true", help="dump the payload instead")
    args = ap.parse_args()
    if args.print:
        print(json.dumps(build_dashboard(), ensure_ascii=False, indent=2))
        return
    p = write_dashboard()
    print(f"écrit : {p}" if p else "échec de l'écriture du tableau de bord")


if __name__ == "__main__":
    main()
