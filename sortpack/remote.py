# -*- coding: utf-8 -*-
"""
Phone → PC command queue + headless auto-apply for the mobile web-app.

Two kinds of work run on the PC (from the GUI's background poll, the "Traiter les
demandes" button, or `python -m sortpack.remote` via Task Scheduler):

1. Queued phone requests (``sort_commands.json`` in the CFB folder) — the phone can't run
   the heavy steps itself (they need BrickStore's local catalog + the .bsx files), so it
   drops a request the PC executes:
       "restants"   → Calculer les restants  (move the "x" lots into the Inventory file
                      and write each leftover's Sac destination).
       "build_cfb"  → Construire fichier CFB  (consolidate + split "x" + write master /
                      restants; emails them when config.REMOTE_CFB_EMAIL).
   The phone's Finances tab queues the same jobs the desktop's "achats & comptabilité"
   buttons run — these carry no set, so they need no folder:
       "sync_purchases" → Synchroniser les achats  (Journal → base + CFB folders).
       "buy_report"     → Rapport d'achat  (scrape → rank → DB → POST au sheet + courriel).
       "cfb_inventory"  → Inventaire CFB  (lire les deux portails, stocker et poster).
       "end_of_month"   → Fin de mois  (recalcule les budgets mensuels depuis la feuille).
       "check_cookies"  → revérifier les cookies CFB (le collage reste sur le PC).
   Each command carries a status the phone reads back (pending → running → done/error).
   Every processed batch refreshes the phone's dashboard.json (sortpack/mobile.py).

2. Auto-apply (no button): writing the remarks is done automatically once a set is DONE
   cataloguing (Inventory ≤ config.CATALOGUE_DONE_FRACTION of the set — 5%, i.e. you have
   finished that set). The decision reads the FILES, not a flag: each set's bags carry a
   fingerprint ("<lots>/<stamped>") and auto_apply.json remembers the one we left behind, so
     • a set already fully remarked is skipped with no work at all (that covers the sets
       applied from the desktop button, which never touches auto_apply.json);
     • a set whose files moved since — re-catalogued, or BrickStore saving an old tab over
       our remarks — is written again;
     • a set whose leftovers Inventory has been through "Calculer les restants" is left
       alone for good: the sorting is physically under way and the Sac layout must not be
       recomputed.
   A set whose ×Quantité isn't known yet (no logged purchase) is skipped.
"""

import os
import json
import uuid
import datetime
import traceback

from . import config
from . import bsx
from . import mobile
from . import lock

COMMANDS_FILE = os.path.join(config.CATALOGUAGE_ROOT, "sort_commands.json")
AUTO_APPLY_FILE = os.path.join(config.CATALOGUAGE_ROOT, "auto_apply.json")
# per-set actions (need the set's folder) vs global jobs (the Finances tab's buttons)
# "restants" (the retired « Appliquer restant ») stays accepted so a phone that has not
# been redeployed yet still gets a real answer instead of "action invalide".
_SET_ACTIONS = ("restants", "build_cfb", "restant_done", "restant_move")
_GLOBAL_ACTIONS = ("sync_purchases", "buy_report", "cfb_inventory", "end_of_month",
                   "check_cookies")
_VALID_ACTIONS = _SET_ACTIONS + _GLOBAL_ACTIONS


# --- small JSON helpers ------------------------------------------------------

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load():
    data = _read_json(COMMANDS_FILE, None)
    if isinstance(data, dict) and isinstance(data.get("commands"), list):
        return data
    return {"commands": []}


def _save(data):
    _write_json(COMMANDS_FILE, data)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def enqueue(key, action, number="", name="", payload=None):
    """Add a pending command (used by tests; the phone writes this via Apps Script)."""
    data = _load()
    cid = uuid.uuid4().hex[:12]
    data["commands"].append({
        "id": cid, "key": str(key), "number": str(number), "name": name,
        "action": action, "created": _now(), "status": "pending",
        "result": "", "finished": "", "payload": payload or {}})
    _save(data)
    return cid


def _update(cmd_id, **fields):
    """Re-read and patch one command by id, so status writes never clobber commands the
    phone appended in the meantime."""
    data = _load()
    for c in data["commands"]:
        if c.get("id") == cmd_id:
            c.update(fields)
            break
    _save(data)


def pending_count():
    return sum(1 for c in _load()["commands"] if c.get("status") == "pending")


# --- set-folder / cataloguing helpers ----------------------------------------

def _resolve_folder(cmd):
    name = cmd.get("name")
    if name:
        p = os.path.join(config.CATALOGUAGE_ROOT, name)
        if os.path.isdir(p):
            return p
    num = str(cmd.get("number") or cmd.get("key") or "").strip()
    if num:
        from .plan import iter_set_folders
        for p in iter_set_folders():        # racine ou dossier de batch
            if os.path.basename(p).startswith(num):
                return p
    return None


def _catalogue_done(folder):
    """True once the leftovers Inventory file is ≤ CATALOGUE_DONE_FRACTION of the set
    (Inventory + numbered bags) — i.e. cataloguing is essentially finished."""
    inv = bags = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    for n in names:
        if not n.lower().endswith(".bsx"):
            continue
        q = bsx.total_qty(os.path.join(folder, n))
        if any(n.startswith(p) for p in config.SKIP_PREFIXES):
            inv += q
        else:
            bags += q
    base = inv + bags
    return base > 0 and inv <= config.CATALOGUE_DONE_FRACTION * base


def _bag_paths(folder):
    """The set's numbered bag files (every .bsx that isn't the leftovers Inventory) — the
    same rule as plan.discover_bags, but paths only, with no XML parse."""
    import glob
    out = []
    for path in glob.glob(os.path.join(folder, "*.bsx")):
        base = os.path.basename(path)
        if any(base.startswith(p) for p in config.SKIP_PREFIXES):
            continue
        out.append(path)
    return out


def coverage(folder):
    """(lots, stamped) across the set's numbered bags — the fingerprint the auto-apply pass
    decides on. Text-only, so it can be asked on every poll."""
    lots = stamped = 0
    for path in _bag_paths(folder):
        n, k = bsx.remark_coverage(path)
        lots += n
        stamped += k
    return lots, stamped


def already_remarked(folder):
    """True when every lot of every numbered bag already carries a remark — the set has been
    applied (by an earlier pass, or by the desktop "Appliquer" button, which doesn't write
    auto_apply.json), so rewriting the same remarks would be pointless work."""
    lots, stamped = coverage(folder)
    return lots > 0 and stamped == lots


def restants_done(folder):
    """True once the leftovers Inventory file carries remarks — what the retired
    « Calculer les restants » pass used to stamp. Kept for the sets that went through it."""
    from .plan import find_inventory
    inv = find_inventory(folder)
    return bool(inv and bsx.has_remarks(inv))


def _batch_name_of(folder):
    """Le nom du batch qui possede ce set, FERME COMPRIS, ou None. Best-effort.

    Fermer un envoi veut dire « plus rien n'entre », pas « ce set redevient autonome » : il
    appartient a l'envoi jusqu'a `batch.finish()`. Ne regarder que les batchs ouverts
    laissait l'auto-apply par set renumeroter un set d'envoi ferme des la fin de son
    cataloguage — avec une numerotation a lui, pas celle de l'envoi."""
    try:
        from . import batch as _batch
        from .plan import set_number
        return _batch.owning_batch(set_number(folder))
    except Exception:
        return None


def in_batch(folder):
    """Ce set appartient-il a un envoi ?

    Si oui, il ne doit jamais etre planifie seul : sa numerotation de Sacs vient du batch
    (une seule sequence sur tout l'envoi), et un Apply par set l'ecraserait avec une
    numerotation propre au set. C'est `batch.auto_apply_all` qui s'en occupe. Best-effort :
    un registre illisible ne bloque rien."""
    return _batch_name_of(folder) is not None


def sorting_started(folder):
    """True once the physical sorting is under way. From that point the Sac layout must never
    be recomputed — auto-apply keeps its hands off the set for good, because renumbering a Sac
    the sorter has already filled contradicts work that cannot be undone.

    This used to be « the leftovers file has remarks », i.e. purely a side effect of the
    « Appliquer restant » button. With that button gone the flag would never be raised again
    and auto-apply would happily re-apply sets mid-sort, so the signal is now the phase you
    actually drive:

      * a bag ticked off, or the phase set to « Trier » by hand — set_status.json, written by
        both the desktop and the phone;
      * plus the legacy stamp, so the sets that already went through the old pass keep theirs.
    """
    if restants_done(folder):
        return True
    from .plan import set_number
    state = _read_json(config.SET_STATUS_FILE, {})
    entry = (state or {}).get(set_number(folder)) or {}
    return bool(entry.get("bags_done") or entry.get("status") == config.STATUS_TRIER)


# --- the queued actions ------------------------------------------------------

def _do_restants(folder, wdb, backup):
    from . import restants
    from .plan import find_inventory
    if find_inventory(folder) is None:
        raise RuntimeError("aucun fichier « Inventory… » dans ce set")
    res = restants.compute_restants(folder, wdb, backup=backup)
    return (f"Restants : {res['moved_lots']} lot(s) « x » déplacé(s) "
            f"({res['moved_qty']} pièces) ; inventaire {res['inventory_lots']} lots / "
            f"{res['inventory_qty']} pièces.")


def _earlier_note(res):
    """« a row in a bag you have already sorted changed » — the one thing a second sorter
    cannot see for themselves, and the reason to reopen that file."""
    e = res.get("earlier_changed") or {}
    if not e:
        return ""
    n = sum(e.values())
    return (f" \u26a0 {n} remarque(s) ont change dans des sacs deja tries "
            f"({', '.join(sorted(e))}) — rouvre ces fichiers.")


def _do_restant_done(folder, payload, wdb):
    """« Fait » from the phone: the leftover is in hand at the bag being sorted, so it leaves
    the inventory and JOINS that bag — the pieces stay in the set (the CFB master would ship
    short otherwise) and the re-plan says which box or Sac they go to."""
    from . import restants
    p = payload or {}
    key = p.get("key")
    if not key:
        raise RuntimeError("pièce manquante dans la demande")
    if p.get("to_bag") is None:
        raise RuntimeError("sac de destination manquant dans la demande — mets l'appli du "
                           "téléphone à jour (le restant doit être placé dans un sac)")
    res = restants.place_in_bag(folder, key, int(p["to_bag"]), qty=p.get("qty"), wdb=wdb)
    msg = (f"Restant {res['key']} : {res['placed_physical']} placée(s) dans le sac "
           f"{res['to_label']} → {res['remark'] or 'sans remarque'}.")
    if not res["cleared"]:
        msg += f" {res['left_physical']} encore en inventaire."
    if res["remarks_changed"]:
        msg += f" {res['remarks_changed']} remarque(s) recalculée(s)."
    return msg + _earlier_note(res)


def _do_restant_move(folder, payload, wdb):
    """« La traiter ici » — the part is in your hand and belongs in the bag you are sorting.

    Two sources, because the phone offers the same gesture for both:
      * a lot catalogued in a LATER bag -> transfer_to_bag drains it forward;
      * a part no file lists any more (`source: "catalog"`) -> add_from_catalog recreates it,
        bounded by what BrickStore says the set contains.
    """
    from . import restants
    p = payload or {}
    if not p.get("key") or p.get("to_bag") is None:
        raise RuntimeError("pièce ou sac manquant dans la demande")
    if p.get("source") == "catalog":
        res = restants.add_from_catalog(folder, p["key"], int(p["to_bag"]),
                                        qty=p.get("qty"), wdb=wdb)
        msg = (f"Rajouté {res['added_physical']} × {res['key']} au sac {res['to_label']} "
               f"→ {res['remark'] or 'sans remarque'}.")
        if res["left_physical"]:
            msg += f" Reste {res['left_physical']} à retrouver."
        if res.get("snapped_from"):
            msg += (f" (arrondi de {res['snapped_from']} à {res['added_physical']} : "
                    f"{res['multiplier']} exemplaires)")
        if res["remarks_changed"]:
            msg += f" {res['remarks_changed']} remarque(s) recalculée(s)."
        return msg + _earlier_note(res)
    res = restants.transfer_to_bag(folder, p["key"], int(p["to_bag"]),
                                   qty=p.get("qty"), wdb=wdb)
    msg = (f"Déplacé {res['qty']} × {res['key']} vers le sac {res['to_label']} "
           f"({', '.join(res['from'])}) → {res['remark'] or 'sans remarque'}.")
    if res.get("snapped_from"):
        msg += f" (arrondi de {res['snapped_from']} à {res['qty']} : {res['multiplier']} exemplaires)"
    if res["remarks_changed"]:
        msg += f" {res['remarks_changed']} autre(s) remarque(s) recalculée(s)."
    return msg + _earlier_note(res)


def _prune_phone_files(set_no, log=None):
    """Drop the derived files that outlive a set whose folder is gone: its leftovers helper,
    and its row in the phone's index / the sorting status / the auto-apply tracking. The
    poller does not rebuild sort_index.json (the desktop does), so its row is removed here —
    otherwise the phone would keep listing a set with no folder behind it."""
    try:
        p = os.path.join(config.CATALOGUAGE_ROOT, config.BAG_HELP_FILE.format(set=set_no))
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
    for path, drop in ((config.SORT_INDEX_FILE, "sets"),
                       (config.SET_STATUS_FILE, None),
                       (AUTO_APPLY_FILE, None)):
        try:
            data = _read_json(path, None)
            if not isinstance(data, dict):
                continue
            if drop:
                rows = [s for s in (data.get(drop) or [])
                        if str(s.get("number") or s.get("key")) != str(set_no)]
                if len(rows) == len(data.get(drop) or []):
                    continue
                data[drop] = rows
            elif str(set_no) in data:
                del data[str(set_no)]
            else:
                continue
            _write_json(path, data)
        except Exception:
            if log:
                log(f"  (nettoyage de {os.path.basename(path)} ignoré)")


def _finish_set(folder, res, log=None):
    """The desktop's « Traiter le set ? », done from the phone: the master has been emailed,
    so the copies leave stock and the folder goes to the Recycle Bin.

    Order matters. The stock row goes first because it is idempotent and reversible, so a
    re-send cannot double-count and a failure here leaves the files untouched. The folder is
    last, because it is the one step with no undo beyond the bin."""
    if not getattr(config, "CFB_PHONE_AUTO_PROCESS", True):
        return ""
    if res.get("verify_gap"):
        return (" ⚠ Set NON traité : la vérification a laissé un écart — le dossier est "
                "gardé (c'est la seule preuve). Termine-le sur le PC.")
    from . import history, trash
    set_no = res["set_number"]
    out = ""
    h = history.HistoryDB()
    try:
        removed = h.send_to_cfb(set_no)
    finally:
        h.close()
    if removed:
        out += f" Inventaire : {removed} copie(s) retirée(s)."
    try:
        trash.send_to_recycle_bin(folder)
        out += " Dossier envoyé à la corbeille."
        _prune_phone_files(set_no, log=log)
    except Exception as e:
        out += f" ⚠ Dossier NON supprimé ({e}) — fais-le sur le PC."
    return out


def _do_build_cfb(folder, email):
    from . import finalize
    # Un set de batch part avec tout l'envoi : construire son maitre seul donnerait une
    # liste partielle, et le client recevrait deux listes au lieu d'une.
    b = _batch_name_of(folder)
    if b:
        raise RuntimeError(f"ce set appartient au batch « {b} » — construis le CFB du "
                           f"batch (bouton « Construire le CFB du batch » sur le PC)")
    lines = []
    res = finalize.build_cfb_files(folder, log=lines.append)
    msg = (f"CFB : {res['master_lots']} lots / {res['master_qty']} pièces"
           f"{' + inventaire' if res.get('included_inventory') else ''}.")
    if res["restants_lots"]:
        msg += f" Écartés « x » : {res['restants_lots']} lots."
    # What the pre-send check did travels back to the phone: a rescaled inventory or an
    # uncorrected gap changes what is in that master, so it must not stay in a log nobody
    # reads — the master is already gone by the time you would look.
    for w in res.get("warnings", []):
        msg += " " + w
    if email and config.CFB_APPS_SCRIPT_URL:
        paths = [p for p in (res["master_path"], res["restants_path"]) if p]
        reply = finalize.email_files(paths, subject=f"CFB — set {res['set_number']}")
        msg += f" Courriel envoyé ({reply})."
        # The set is out the door, so finish it exactly as the desktop would. Only ever
        # after a mail that actually went: a build alone changes nothing.
        msg += _finish_set(folder, res, log=lines.append)
    elif email:
        msg += " (Apps Script non configuré — courriel non envoyé.)"
    return msg


# --- the global jobs (the phone's Finances tab) ------------------------------
# Each mirrors one desktop button in "MLJQ — achats & comptabilité" and returns the one-line
# result the phone shows under the button. They run on the PC because they need the local
# catalog / price guide and the sheet token.

def _do_sync_purchases(log):
    """Journal (Catégorie-A) → bought table + missing CFB folders. Same as the desktop
    "Synchroniser les achats"."""
    from .catalogdb import CatalogDB
    from .history import HistoryDB
    from . import purchases
    cat, hist = CatalogDB(), HistoryDB()
    try:
        res = purchases.sync(cat, hist, log=log)
    finally:
        hist.close()
    msg = (f"Achats synchronisés : {res.get('journal', 0)} achat(s) miroir sur "
           f"{res.get('transactions', 0)} transaction(s).")
    if res.get("new"):
        msg += f" {res['new']} nouveau(x) set(s) — dossiers CFB créés."
    if res.get("unmatched"):
        msg += f" ⚠ {res['unmatched']} ligne(s) ignorée(s) (numéro introuvable)."
    return msg


def _do_buy_report(log):
    """The full headless pipeline + POST to the sheet (and the "lot du mois" email when the
    batch changed). Same as the desktop "Rapport d'achat"."""
    from . import job
    s = job.run(post=True, log=log)
    msg = (f"Rapport d'achat : {s['amazon']} Amazon + {s['costco']} Costco · "
           f"{s['candidates']} candidats · lot de {s['chosen']} set(s) "
           f"({s['batch_cost']:,.0f}$, profit/an {s['batch_annual_profit']:,.0f}$).")
    msg += " Posté au sheet." if s.get("posted") else " (POST non configuré.)"
    if s.get("warning"):
        msg += " ⚠ " + s["warning"]
    return msg


def _do_cfb_inventory(log):
    """Read both portals for every configured seller, store the snapshot and post it to the
    month's rows. Same as the desktop "Inventaire CFB"."""
    from . import cfb_inventory
    results = cfb_inventory.capture(post=True, log=log)
    if not results:
        return ("Inventaire CFB : aucun vendeur lisible (pas encore sur le portail ?) — "
                "rien enregistré.")
    bits = [f"{r.get('vendor') or '?'} {r.get('value') or 0:,.0f}$" for r in results]
    return (f"Inventaire CFB (au {results[0].get('as_of')}) : " + " · ".join(bits)
            + " — enregistré et posté.")


def _do_end_of_month(log):
    """Re-read the financial sheet, lock in each month's investable budget, and report what
    is available now. Same as the desktop "Fin de mois"."""
    from . import job
    from .history import HistoryDB
    h = HistoryDB()
    try:
        rate, stored = job.refresh_budget(h, log=log, post=True)
        avail, _ = h.available_budget(config.MONTHLY_BUDGET_CAD, config.BUDGET_ANCHOR_MONTH)
        spent = h.spent_since(config.BUDGET_ANCHOR_MONTH)
    finally:
        h.close()
    head = (f"Fin de mois : {len(stored)} mois calculé(s)"
            + (f" (taux MLJQ {rate:.0%})" if rate else " (repli budget fixe)"))
    return (f"{head}. Reste à investir {avail:,.2f}$ "
            f"(déjà dépensé {spent:,.2f}$ depuis {config.BUDGET_ANCHOR_MONTH}).")


def _do_check_cookies(log):
    """Re-probe both CFB portals and remember when. Pasting fresh cookies stays a desktop
    job ("Nouveaux cookies") — the phone only reports and re-checks."""
    from . import cfb_inventory
    from .history import HistoryDB
    results = cfb_inventory.check_and_alert(log=log)
    h = HistoryDB()
    try:
        for r in results:
            h.set_meta(f"cfb_checked_{r['source']}", _now())
    finally:
        h.close()
    bits = [f"{r['source']} : " + ("OK" if r["ok"] else "EXPIRÉ") for r in results]
    bad = [r for r in results if not r["ok"]]
    msg = "Cookies CFB — " + " · ".join(bits)
    if bad:
        msg += " — colle les nouveaux sur le PC (« Nouveaux cookies »)."
    return msg


_GLOBAL_HANDLERS = {
    "sync_purchases": _do_sync_purchases,
    "buy_report": _do_buy_report,
    "cfb_inventory": _do_cfb_inventory,
    "end_of_month": _do_end_of_month,
    "check_cookies": _do_check_cookies,
}


def _safe_logger(log):
    """Wrap a log callable so a console that can't encode « ✔ » or accents (cp1252 when the
    task runs outside the GUI) can never turn a finished job into an error."""
    def _log(msg):
        try:
            log(msg)
        except Exception:
            try:
                log(str(msg).encode("ascii", "replace").decode("ascii"))
            except Exception:
                pass
    return _log


def process_pending(log=print, backup=True, email=None, wdb=None, force=False):
    """Run every pending command once. Returns (done, errors).

    Stands down unless this machine owns the shared folder (sortpack/lock.py); `force` takes
    the lock first, which is what the GUI's "Demandes" button does - asking this PC to do the
    work is exactly a request for it to own it."""
    log = _safe_logger(log)
    if force:
        lock.acquire(force=True, log=log)
    elif not lock.owns(log=log):
        return 0, 0
    if email is None:
        email = getattr(config, "REMOTE_CFB_EMAIL", False)

    # Une phase posee depuis le TELEPHONE ne passe pas par request_trier : l'app ecrit
    # set_status.json en direct. C'est ici qu'on la rattrape — un set pousse a « Trier » sur
    # un envoi pas encore ferme redescend en attente, et un envoi qu'on vient de fermer voit
    # ses remarques s'ecrire. Le poll tourne de toute facon : autant qu'il reconcilie.
    try:
        from . import deliveries as _deliv
        _deliv.settle(log=log)             # un colis livre sort le set de l'attente
    except Exception as e:
        log(f"  ⚠ livraisons non traitées : {str(e)[:80]}")
    try:
        from . import batch as _batch
        _batch.settle_phases(log=log)
    except Exception as e:
        log(f"  ⚠ phases non réconciliées : {str(e)[:80]}")

    todo = [c for c in _load()["commands"] if c.get("status") == "pending"]
    if not todo:
        return 0, 0

    done = errors = 0
    _wdb = wdb
    for cmd in todo:
        cid, action = cmd.get("id"), cmd.get("action")
        if action not in _VALID_ACTIONS:
            _update(cid, status="error", result=f"action inconnue : {action}", finished=_now())
            errors += 1
            continue
        _update(cid, status="running")
        is_global = action in _GLOBAL_ACTIONS
        label = (action if is_global
                 else (cmd.get("name") or cmd.get("number") or cmd.get("key")))
        try:
            if is_global:
                result = _GLOBAL_HANDLERS[action](log)
                mobile.mark_run(action)
            else:
                folder = _resolve_folder(cmd)
                if folder is None:
                    raise RuntimeError("dossier du set introuvable")
                if action in ("restants", "restant_done", "restant_move"):
                    if _wdb is None:
                        log("Chargement du catalogue BrickStore (poids)…")
                        from .weightdb import WeightDB
                        _wdb = WeightDB()
                    if action == "restants":
                        result = _do_restants(folder, _wdb, backup)
                    elif action == "restant_done":
                        result = _do_restant_done(folder, cmd.get("payload"), _wdb)
                    else:
                        result = _do_restant_move(folder, cmd.get("payload"), _wdb)
                else:
                    result = _do_build_cfb(folder, email)
            _update(cid, status="done", result=result, finished=_now())
            log(f"✔ {action} — {label} : {result}")
            done += 1
        except Exception as e:
            traceback.print_exc()
            _update(cid, status="error", result=str(e)[:200], finished=_now())
            log(f"⚠ {action} — {label} : {e}")
            errors += 1
    # every batch can have moved money, stock or the budget — refresh the phone's snapshot
    mobile.write_dashboard()
    return done, errors


# --- auto-apply (headless, no button) ----------------------------------------

def _load_applied():
    d = _read_json(AUTO_APPLY_FILE, {})
    return d if isinstance(d, dict) else {}


def _fingerprint(folder):
    """A cheap signature of a set's bag files: "<lots>/<stamped>". Auto-apply stores the one
    it left behind, so it can tell "nothing changed since I wrote" (skip) from "the files
    moved under me" (write again) without trusting a flag."""
    lots, stamped = coverage(folder)
    return f"{lots}/{stamped}"


def _stored_fingerprint(entry):
    """The fingerprint inside an auto_apply.json entry. Entries written before this existed
    are a bare timestamp string: they return None and get upgraded on the next pass."""
    if isinstance(entry, dict):
        return entry.get("fp")
    return None


def _apply_action(folder, applied):
    """What the auto-apply pass should do with one set:
         "apply"  - write the remarks now
         "record" - already fully remarked, just remember it (no work, no backup)
         "forget" - tracked but no longer done cataloguing: re-arm it
         "skip"   - nothing to do
    The decision reads the FILES, not a flag: a set whose fingerprint still matches what we
    left behind is skipped, and one whose bags lost their remarks (BrickStore saving an old
    tab over them, a re-export) is simply written again."""
    from .plan import set_number
    key = set_number(folder)
    if not _catalogue_done(folder):
        return "forget" if key in applied else "skip"
    if sorting_started(folder):
        return "skip"                  # sorting started - never renumber the Sacs
    if in_batch(folder):
        return "skip"                  # l'envoi le planifie, pas nous (batch.auto_apply_all)
    entry = applied.get(key)
    if entry is not None and _stored_fingerprint(entry) == _fingerprint(folder):
        return "skip"                  # unchanged since we wrote it
    if already_remarked(folder):
        return "record"
    return "apply"


def _has_autoapply_candidate():
    """Cheap check (no BrickStore weights loaded): is any set actually waiting to be written?
    Lets the poller decide whether to spin up the heavy pass."""
    if not getattr(config, "REMOTE_AUTO_APPLY", True):
        return False
    root = config.CATALOGUAGE_ROOT
    if not os.path.isdir(root):
        return False
    from .plan import iter_set_folders
    applied = _load_applied()
    for folder in iter_set_folders(root):      # racine + dossiers de batch
        if _apply_action(folder, applied) == "apply":
            return True
    try:
        from . import batch as _batch
        return _batch.has_auto_apply_candidate()
    except Exception:
        return False


def auto_apply_ready(log=print, wdb=None, backup=True):
    """Write the remarks for every set that is done cataloguing and isn't already written.
    Returns the number of sets applied. Safe to run at any time - including while BrickStore
    is open: a set is written only when its own files say it needs it, and if BrickStore ever
    saves an old tab over the result, the next pass sees the lost remarks and rewrites
    them."""
    if not getattr(config, "REMOTE_AUTO_APPLY", True):
        return 0
    if not lock.owns(log=log):
        return 0                       # another machine owns the shared folder
    root = config.CATALOGUAGE_ROOT
    if not os.path.isdir(root):
        return 0
    from .plan import is_set_folder, set_number, build_plan
    from .apply import apply_plan

    # Un set acheté pour un envoi y est inscrit dès la synchro ; c'est ici qu'il rejoint
    # physiquement son batch, une fois son cataloguage terminé.
    try:
        from . import batch as _batch
        _batch.settle(log=log)
    except Exception as e:
        log(f"⚠ rattachement aux batchs ignoré : {str(e)[:70]}")

    applied = _load_applied()
    changed = n = 0
    _wdb = wdb
    _cat = None                        # catalog for the pre-Apply verification (lazy)
    from .plan import iter_set_folders
    for folder in iter_set_folders(root):      # racine + dossiers de batch
        name = os.path.basename(folder)
        key = set_number(folder)
        action = _apply_action(folder, applied)
        if action == "skip":
            continue
        if action == "forget":         # re-cataloguing re-arms the auto-apply
            del applied[key]
            changed = 1
            continue
        if action == "record":
            # applied before we tracked it, or from the desktop button - remember the state
            # so the check costs nothing next time; no catalog load, no backup, no rewrite.
            applied[key] = {"at": _now(), "fp": _fingerprint(folder)}
            changed = 1
            log(f"• Auto-apply {name} : déjà annoté — rien à réécrire.")
            continue
        try:
            if _wdb is None:
                log("Chargement du catalogue BrickStore (poids)…")
                from .weightdb import WeightDB
                _wdb = WeightDB()
            if getattr(config, "VERIFY_ON_APPLY", True):
                from . import verify
                if _cat is None:
                    log("Chargement du catalogue BrickStore (inventaires)\u2026")
                    from .catalogdb import CatalogDB
                    _cat = CatalogDB()
                verify.before_apply(folder, cat=_cat, log=log, backup=backup)
            plan = build_plan(folder, _wdb)
            if config.CFB_MULTIPLY_FROM_SHEET and not plan["stats"].get("mult_ok", True):
                log(f"Auto-apply {name} : ×Quantité inconnue — ignoré (logue l'achat).")
                continue
            total, _ = apply_plan(folder, plan, backup=backup)
            applied[key] = {"at": _now(), "fp": _fingerprint(folder)}
            changed = 1
            n += 1
            log(f"✔ Auto-apply {name} : {total} lots écrits (×{plan['stats']['multiplier']}).")
        except Exception as e:
            traceback.print_exc()
            log(f"⚠ Auto-apply {name} : {e}")
    if changed:
        _write_json(AUTO_APPLY_FILE, applied)

    # Les sets d'envoi ont ete sautes un par un ci-dessus : c'est l'ENVOI qui les planifie,
    # tous ensemble, pour n'avoir qu'une numerotation de Sacs. Sa passe tourne ici, avec le
    # meme catalogue deja charge et sous le meme verrou.
    try:
        from . import batch as _batch
        n += _batch.auto_apply_all(wdb=_wdb, backup=backup, log=log)
    except Exception as e:
        traceback.print_exc()
        log(f"⚠ Auto-apply des envois : {str(e)[:120]}")
    return n


def has_work():
    """True if there's anything for the PC to do now (a queued request, or a set waiting to
    be written). Cheap — used by the GUI poll to decide."""
    if not lock.owns():
        return False                   # another machine is doing it
    if pending_count() > 0:
        return True
    if getattr(config, "REMOTE_AUTO_APPLY", True):
        return _has_autoapply_candidate()
    return False


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Run the phone→PC queue and the auto-apply pass once.")
    ap.add_argument("--no-backup", action="store_true", help="skip the pre-write backup")
    ap.add_argument("--no-email", action="store_true", help="don't email on remote CFB build")
    ap.add_argument("--no-auto-apply", action="store_true", help="skip the auto-apply pass")
    ap.add_argument("--take-lock", action="store_true",
                    help="claim the shared-folder lock for this machine first")
    args = ap.parse_args()
    backup = not args.no_backup
    email = False if args.no_email else None
    done, errors = process_pending(backup=backup, email=email, force=args.take_lock)
    applied = 0 if args.no_auto_apply else auto_apply_ready(backup=backup)
    mobile.maybe_write_dashboard()      # keep the phone's Finances tab from going stale
    print(f"queue: done={done} errors={errors} pending_left={pending_count()} | "
          f"auto_applied={applied} | {lock.describe()}")


if __name__ == "__main__":
    main()
