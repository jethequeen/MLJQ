# -*- coding: utf-8 -*-
"""
Process LEGO purchases logged in the accounting sheet's Journal (Catégorie "A"),
e.g. "Amazon - 76342 (10)". Every sync MIRRORS the Journal into the `bought` history
table — so edits and deletions in the sheet are reflected, not just appends — then, for
each newly-seen set, creates its CFB folder ("<number> - <Name>") under BSX\\CFB.
Inventory is derived from `bought` (per-set sum). Hand entries / migrations live as
src='manual' rows and are left untouched by the mirror.

Purchases are the source of truth for "don't re-buy a set we bought < REBUY_WINDOW
months ago" — see buylist.rank / job.run. (The later "sent to CFB" side — decrement
stock, remove folder — is a future flow.)
"""

import os
import re
import glob

from . import config
from . import sheet
from . import bsx

# "<vendor> - <setnumber> (<qty>)"  e.g. "Amazon - 76342 (10)"
_TXN_RE = re.compile(r"^\s*(.*?)\s*-\s*(\d{3,7})\s*\((\d+)\)\s*$")
_BAD_FS = re.compile(r'[<>:"/\\|?*]')


def parse_transaction(text):
    """('Amazon', '76342', 10) from 'Amazon - 76342 (10)', or (None, None, None)."""
    m = _TXN_RE.match(text or "")
    if not m:
        return None, None, None
    return m.group(1).strip() or None, m.group(2), int(m.group(3))


def _to_float(v):
    """Sheet cell → float, tolerating numbers and strings like '(455,00)', '$1 234,56'."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(" ", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return 0.0
    return -f if neg else f


def net_amount(txn):
    """Pre-tax CAD of a transaction dict: |Montant| − |TPS| − |TVQ| (tax is an ITC)."""
    return (abs(_to_float(txn.get("montant")))
            - abs(_to_float(txn.get("tps"))) - abs(_to_float(txn.get("tvq"))))


def _parent_folder_for(set_number, log=print):
    """Où doit naître le dossier de ce set : dans le dossier de son envoi s'il en a déjà un,
    sinon à la racine du cataloguage.

    Un set est souvent inscrit à un envoi AVANT que son colis arrive (`_attach_to_batch` le
    fait le jour de l'achat, depuis le classement). Créer quand même son dossier à la racine
    le laissait dehors avec son inventaire, à part dans la liste comme dans l'explorateur —
    c'est ce qui est arrivé au Jaguar de l'envoi 1. Le batch possède ses sets : son dossier
    est leur place, dès le premier jour."""
    try:
        from . import batch as _batch
        name = _batch.owning_batch(set_number)
        if name:
            return _batch.batch_dir(name, create_it=True)
    except Exception as e:
        log(f"  ⚠ dossier d'envoi indisponible pour {set_number} : {str(e)[:70]}")
    return config.CATALOGUAGE_ROOT


def _folder_for(catalog, canonical_set_id, log=print):
    """Create the set folder INSIDE its batch folder when it already belongs to one, else
    under BSX\\CFB\\. Returns the path (or None). Also drops a fresh full-set inventory
    .bsx into the folder (config.AUTO_CREATE_INVENTORY) so cataloguing starts from the
    whole set."""
    si = catalog.set_info(canonical_set_id)
    if not si:
        return None
    number = canonical_set_id.split("-")[0]
    name = _BAD_FS.sub("", si.name or "").strip()
    path = os.path.join(_parent_folder_for(number, log=log),
                        f"{number} - {name}".rstrip(" -"))
    os.makedirs(path, exist_ok=True)
    _ensure_inventory_file(catalog, canonical_set_id, path, log=log)
    return path


def _ensure_inventory_file(catalog, canonical_set_id, folder, log=print):
    """Write 'Inventory for <set-id>.bsx' (the full set, base quantities) into `folder` if
    it has no Inventory*.bsx yet. Skipped when disabled or already present; never fatal."""
    if not getattr(config, "AUTO_CREATE_INVENTORY", False):
        return None
    if glob.glob(os.path.join(folder, "Inventory*.bsx")):
        return None
    try:
        # Les pieces en trop sont DANS la boite et on les catalogue : c'est la reference de
        # verify.py (« spares INCLUDED ») et c'est ce que le maitre expedie. Les exclure ici
        # faisait demarrer le cataloguage sur un inventaire plus court que la realite, que
        # verify remettait ensuite — et donnait deux comptes de pieces pour un meme set selon
        # que son dossier existait ou non (16 730 contre 17 400 pour le Jaguar x10).
        parts = catalog.inventory(canonical_set_id, include_extras=True)
        if not parts:
            return None
        out_path = os.path.join(folder, f"Inventory for {canonical_set_id}.bsx")
        bsx.write_inventory(out_path, parts)
        log(f"  Inventaire créé : {os.path.basename(out_path)} ({len(parts)} lots)")
        return out_path
    except Exception as e:
        log(f"  ⚠ inventaire non créé pour {canonical_set_id} : {str(e)[:80]}")
        return None


def sync(catalog, hist, create_folders=True, log=print):
    """Mirror the Journal's Catégorie-'A' transactions into the `bought` table so edits
    and deletions in the sheet are reflected (not just appends), then make CFB folders for
    newly-seen sets. Returns a summary dict. Safe to run every job (idempotent)."""
    try:
        txns = sheet.fetch_transactions()
    except Exception as e:
        log(f"  ⚠ achats non synchronisés (sheet : {str(e)[:100]})")
        return {"transactions": 0, "new": 0, "unmatched": 0}

    rows, unmatched = [], 0
    for t in txns:
        vendor, set_no, qty = parse_transaction(t.get("transaction", ""))
        if not set_no or not catalog.has_set(set_no):
            unmatched += 1
            continue
        canonical = catalog.set_info(set_no).set_id
        rows.append((t.get("date", ""), canonical, qty, vendor, round(net_amount(t), 2)))

    n_journal, added = hist.reconcile_journal(rows)
    if create_folders:
        for canonical in added:
            try:
                _folder_for(catalog, canonical, log=log)
            except Exception as e:
                log(f"  ⚠ dossier CFB non créé pour {canonical} : {str(e)[:80]}")
    _mark_awaiting_delivery(added, log=log)
    joined = _attach_to_batch(hist, added, log=log)
    return {"transactions": len(txns), "new": len(added), "journal": n_journal,
            "unmatched": unmatched, "batched": joined}


def _mark_awaiting_delivery(added, log=print):
    """Un set qu'on vient d'acheter est « En attente de livraison », pas « Cataloguer ».

    La synchro lui crée un dossier et un inventaire dès l'achat, ce qui le faisait apparaître
    prêt à cataloguer alors que la boîte est encore chez le transporteur. La phase tombe toute
    seule quand un courriel de livraison arrive (sortpack/deliveries.py)."""
    if not added:
        return []
    from . import remote as _remote
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    touched = []
    for canonical in added:
        key = str(canonical).split("-")[0]
        entry = state.get(key) or {}
        if entry.get("status"):
            continue                      # une phase existe déjà : on ne recule jamais
        entry["status"] = config.STATUS_LIVRAISON
        state[key] = entry
        touched.append(key)
    if touched:
        _remote._write_json(config.SET_STATUS_FILE, state)
        log("  %s : %s" % (", ".join(touched), config.STATUS_LIVRAISON))
    return touched


def _attach_to_batch(hist, added, log=print):
    """Inscrit au batch en cours les sets qu'on vient d'acheter POUR lui.

    La liste d'achat ne propose pas des sets au hasard : elle choisit de quoi compléter un
    envoi précis (buylist.open_batch_target). Sans ce rattachement, on achèterait pour le
    batch 1, les dossiers apparaîtraient à la racine, le compteur du batch resterait au même
    chiffre — et la liste d'achat proposerait de le remplir une deuxième fois.

    On n'inscrit que ce qui était RETENU dans le dernier classement : un set acheté pour une
    autre raison n'a rien à faire dans l'envoi. L'inscription emporte le dossier avec elle
    (batch.add → _move_into), et les achats suivants le créeront directement dedans.
    """
    if not added:
        return []
    try:
        from . import batch as _batch, buylist
        from .history import BUY_SHEET_COLS
    except Exception:
        return []
    name = buylist.open_batch_target()[0]
    if not name:
        return []                        # aucun envoi en cours : rien à rattacher
    try:
        idx = BUY_SHEET_COLS.index("set_id")
        chosen = {str(r[idx]) for r in hist.latest_buy_list(chosen_only=True)}
    except Exception:
        return []
    joined = []
    for canonical in added:
        if str(canonical) not in chosen:
            continue
        num = str(canonical).split("-")[0]
        if _batch.batch_of(num):
            continue                     # déjà dans un envoi
        try:
            _batch.add(name, num)
            joined.append(num)
            log(f"  {num} inscrit au batch « {name} » (acheté pour cet envoi) — "
                f"son dossier y est rangé.")
        except Exception as e:
            log(f"  ⚠ {num} non inscrit au batch « {name} » : {str(e)[:70]}")
    return joined
