# -*- coding: utf-8 -*-
"""
Cross-check a set's files against the set's real inventory, and fix the quantities.

Cataloguing by hand means moving parts out of the leftovers ``Inventory*.bsx`` and into the
numbered bag files. Decrementing one and incrementing the other is where a piece gets lost
or counted twice. This module compares what the files hold against what BrickStore's own
catalog says the set contains, and puts the difference back.

**The reference** is ``CatalogDB.inventory(set_no, include_extras=True)`` at BASE quantity
(one copy). Extras are included on purpose: the box physically contains its spare parts and
you catalogue them, so leaving them out would flag every spare as surplus. Verified on three
finished sets — 42680 (89 lots), 43011 (219), 77256 (150) — which reconcile exactly this way.

**Scale.** Every file is read at its own multiplication state, the Inventory included: it is
created at base quantity, and ``normalize_inventory`` brings it to the bags' physical ×N once
they are multiplied (the CFB master folds the inventory in as-is, so a base lot would ship at
1/N).

**What it holds** = ``Σ lots ÷ N``, where N is each file's
multiplication marker (``MLJQ-xN``, written at Apply). At Apply time nothing is multiplied
yet, so N is 1 everywhere; an already-multiplied set is still *reported* correctly, but it is
never *written* to (see ``can_fix``) — adding "one piece" to a ×9 file is meaningless.

**How a difference is repaired** (per part):
  * missing pieces → add them to the lot in the Inventory file if there is one, otherwise to
    any bag lot, otherwise create a lot in the Inventory file;
  * surplus pieces → take them off the Inventory file first, then the bag lots, dropping a
    lot that reaches zero;
  * a lot the catalog doesn't know at all → removed (config.VERIFY_DROP_UNKNOWN).

A big discrepancy is a sign something is wrong with the *set*, not the count (wrong number,
files from another set), so beyond config.VERIFY_MAX_* the pass reports and writes nothing.
"""

import os
import glob
from collections import defaultdict

from . import config
from . import bsx


def _key(item_type, item_id, color_id):
    """The identity of a lot across catalog and files: type letter, id, colour number.
    Colour arrives as text from the XML and as an int from the catalog, hence the int()."""
    try:
        color = int(color_id or 0)
    except (TypeError, ValueError):
        color = 0
    return (str(item_type or ""), str(item_id or ""), color)


def expected_parts(cat, set_no):
    """{key: (qty, part)} — one copy of the set as the catalog describes it, spares included."""
    out = {}
    for p in cat.inventory(set_no, include_extras=True):
        k = _key(p.item_type, p.item_id, p.color_id)
        if k in out:
            qty, first = out[k]
            out[k] = (qty + p.qty, first)       # same part listed twice (e.g. two sub-builds)
        else:
            out[k] = (p.qty, p)
    return out


class _Lot(object):
    """One lot in one file: where it is, what it holds, and the file's multiplier."""
    __slots__ = ("path", "row", "qty", "mult", "is_inventory", "name")

    def __init__(self, path, row, qty, mult, is_inventory, name):
        self.path, self.row, self.qty = path, row, qty
        self.mult, self.is_inventory, self.name = mult, is_inventory, name


def set_multiplier(set_no):
    """How many copies of the set we own — the factor Apply multiplies the bags by. Same
    source as build_plan (the `inventory` table, fed by the Journal's purchases); 1 when
    unknown, which keeps the comparison at base quantities."""
    if not getattr(config, "CFB_MULTIPLY_FROM_SHEET", False):
        return 1
    from . import history
    h = history.HistoryDB()
    try:
        return h.inventory_qty(set_no) or 1
    finally:
        h.close()


def scan_files(folder, multiplier=1):
    """Read every .bsx of the set. Returns (lots_by_key, files) where files maps a path to
    the factor its quantities are already scaled by.

    Multiplication is detected exactly as build_plan does — the MLJQ-xN marker, or every
    quantity dividing by the multiplier — because BrickStore strips the marker when it saves
    a file (3 of 43011's 8 bags have lost theirs)."""
    lots = defaultdict(list)
    files = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.bsx"))):
        base = os.path.basename(path)
        is_inv = any(base.startswith(p) for p in config.SKIP_PREFIXES)
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            state = bsx.multiplication_state(raw, multiplier)
            mult = 1 if state == "base" else multiplier
            items = bsx.read_items(path)
        except (OSError, ValueError):
            continue
        files[path] = mult
        for it in items:
            k = _key(it["item_type"], it["item_id"], it["color_id"])
            lots[k].append(_Lot(path, it["row"], it["qty"], mult, is_inv,
                                f'{it["item_name"]} / {it["color_name"]}'.strip(" /")))
    return lots, files


def _have_base(lots_for_key):
    """Base-quantity total of one part across the files (each file divided by its own
    multiplier). Returns None when a ×N file doesn't divide evenly — that lot can't be
    reasoned about in base units, so the part is reported and left alone."""
    total = 0
    for lot in lots_for_key:
        if lot.mult > 1:
            if lot.qty % lot.mult:
                return None
            total += lot.qty // lot.mult
        else:
            total += lot.qty
    return total


def _catalogue_unfinished(folder):
    """Reste-t-il du cataloguage a faire dans ce dossier ? (meme reponse que partout
    ailleurs — c'est `remote` qui en decide). False sur un doute : ne jamais bloquer une
    mise a l'echelle a cause d'un fichier illisible."""
    try:
        from . import remote as _remote
        return not _remote._catalogue_done(folder)
    except Exception:
        return False


def normalize_inventory(folder, multiplier, log=None, force=False):
    """Bring the leftovers Inventory file up to the bags' physical scale (xN).

    The file is created at base quantity (one copy) and the bags are multiplied at Apply,
    so an un-normalised inventory ships its lots to CFB at 1/N — finalize folds it into the
    master as-is. Once the bags are multiplied, the inventory must be too. Self-guarded: a
    file already stamped, or already divisible, is left alone. Returns the factor applied.

    **Pas pendant le cataloguage.** Un envoi s'annote maintenant sacs par sacs, sans
    attendre que ses sets soient finis (batch.auto_apply_all) : des le premier Apply, des
    sacs sont multiplies et tampones alors que l'inventaire est encore le PLAN DE TRAVAIL du
    cataloguage — on en sort les pieces d'UN exemplaire, a la main. Le passer a xN sous les
    doigts du cataloguateur lui ferait compter dix fois chaque lot. Un dossier mi-base mi-xN
    reste coherent : `check` lit l'echelle fichier par fichier. L'inventaire sera mis a
    l'echelle au premier Apply qui suivra la fin du cataloguage. `force=True` passe outre —
    pour la construction du maitre, ou l'inventaire DOIT partir a l'echelle physique."""
    from .plan import find_inventory
    if not multiplier or multiplier <= 1:
        return 1
    inv = find_inventory(folder)
    if not inv:
        return 1
    if not force and _catalogue_unfinished(folder):
        return 1
    # only once the bags themselves are multiplied - during cataloguing everything is base
    bags_scaled = False
    for path in sorted(glob.glob(os.path.join(folder, "*.bsx"))):
        base = os.path.basename(path)
        if any(base.startswith(p) for p in config.SKIP_PREFIXES):
            continue
        with open(path, "r", encoding="utf-8") as f:
            if bsx.multiplication_state(f.read(), multiplier) != "base":
                bags_scaled = True
                break
    if not bags_scaled:
        return 1
    n = bsx.multiply_quantities(inv, multiplier,
                                gui_state=getattr(config, "INVENTORY_GUI_STATE", None))
    if n > 1 and log:
        log(f"• Inventaire mis à l'échelle physique (×{n}) — il partait à la quantité "
            f"d'un seul exemplaire.")
    return n


def check(folder, cat, set_no=None, multiplier=None):
    """Compare the files against the catalog. Returns a report dict; writes nothing."""
    from .plan import set_number
    set_no = set_no or set_number(folder)
    if multiplier is None:
        multiplier = set_multiplier(set_no)
    expected = expected_parts(cat, set_no)
    lots, files = scan_files(folder, multiplier)

    multiplied = any(m > 1 for m in files.values())
    inventory_path = None
    for path in files:
        if any(os.path.basename(path).startswith(p) for p in config.SKIP_PREFIXES):
            inventory_path = path
            break

    diffs = []                        # (key, expected, have, part_or_None, lots)
    for k, (qty, part) in expected.items():
        have = _have_base(lots.get(k, []))
        if have is None or have != qty:
            diffs.append((k, qty, have, part, lots.get(k, [])))
    for k, ls in lots.items():
        if k not in expected:
            diffs.append((k, 0, _have_base(ls), None, ls))

    exp_lots = len(expected)
    exp_qty = sum(q for q, _ in expected.values())
    have_qty = sum(sum(l.qty for l in ls) for ls in lots.values())
    gap_qty = sum(abs((h if h is not None else 0) - e) for _, e, h, _, _ in diffs)

    max_lot_frac = getattr(config, "VERIFY_MAX_LOT_FRACTION", 0.25)
    max_qty_frac = getattr(config, "VERIFY_MAX_QTY_FRACTION", 0.15)
    blocked = None
    if multiplied:
        blocked = ("les sacs sont déjà multipliés (×N) — vérification en lecture seule")
    elif exp_lots and len(diffs) > max_lot_frac * exp_lots:
        blocked = (f"{len(diffs)} lots en écart sur {exp_lots} "
                   f"(> {max_lot_frac:.0%}) — rien n'est corrigé, vérifie le set")
    elif exp_qty and gap_qty > max_qty_frac * exp_qty:
        blocked = (f"écart de {gap_qty} pièces sur {exp_qty} attendues "
                   f"(> {max_qty_frac:.0%}) — rien n'est corrigé, vérifie le set")
    elif inventory_path is None and any(h is not None and h < e
                                        for _, e, h, _, _ in diffs):
        blocked = "aucun fichier « Inventory… » où remettre les pièces manquantes"

    return {"set": set_no, "folder": folder, "diffs": diffs, "expected_lots": exp_lots,
            "expected_qty": exp_qty, "have_qty": have_qty, "gap_qty": gap_qty,
            "inventory_path": inventory_path, "multiplied": multiplied,
            "multiplier": multiplier, "can_fix": blocked is None, "blocked": blocked,
            "files": files}


def _plan_fix(report):
    """Turn the differences into concrete per-file edits. Returns
    (qty_by_file {path: {row: qty}}, remove_by_file {path: {rows}}, new_lots [(part, qty)],
     actions [human-readable lines])."""
    qty_by_file = defaultdict(dict)
    remove_by_file = defaultdict(set)
    new_lots = []
    actions = []
    drop_unknown = getattr(config, "VERIFY_DROP_UNKNOWN", True)

    def current(lot):
        """This lot's quantity as already staged by an earlier edit, else on disk."""
        return qty_by_file[lot.path].get(lot.row, lot.qty)

    for key, exp, have, part, lots in report["diffs"]:
        label = (lots[0].name if lots else
                 f'{getattr(part, "item_name", "") or key[1]} / {getattr(part, "color_name", "")}')
        label = (label or "").strip(" /") or f"{key[0]}{key[1]}@{key[2]}"
        if have is None:                       # ×N file that doesn't divide — hands off
            actions.append(f"?  {label} : quantités ×N non divisibles — ignoré")
            continue

        if part is None:                       # the catalog doesn't know this lot at all
            if not drop_unknown:
                actions.append(f"?  {label} : absent du catalogue — signalé, non supprimé")
                continue
            for lot in lots:
                remove_by_file[lot.path].add(lot.row)
            actions.append(f"−  {label} : absent du catalogue — {have} pièce(s) supprimée(s)")
            continue

        diff = exp - have
        if diff > 0:                           # missing: put them back
            target = next((l for l in lots if l.is_inventory), None) \
                or next((l for l in lots if l.mult == 1), None)
            if target is not None:
                qty_by_file[target.path][target.row] = current(target) + diff
                where = ("l'inventaire" if target.is_inventory
                         else os.path.basename(target.path))
                actions.append(f"+  {label} : {have} → {exp} (+{diff} dans {where})")
            else:
                new_lots.append((part, diff))
                actions.append(f"+  {label} : absent des fichiers — {diff} ajouté(s) "
                               f"à l'inventaire")
        else:                                  # surplus: take them off
            left = -diff
            ordered = ([l for l in lots if l.is_inventory]
                       + [l for l in lots if not l.is_inventory and l.mult == 1])
            taken = []
            for lot in ordered:
                if left <= 0:
                    break
                cur = current(lot)
                cut = min(cur, left)
                if cut <= 0:
                    continue
                left -= cut
                if cur - cut == 0:
                    remove_by_file[lot.path].add(lot.row)
                else:
                    qty_by_file[lot.path][lot.row] = cur - cut
                taken.append(f"{cut} de "
                             + ("l'inventaire" if lot.is_inventory
                                else os.path.basename(lot.path)))
            actions.append(f"−  {label} : {have} → {exp} (−{-diff} : " + ", ".join(taken) + ")")
            if left:
                actions.append(f"?  {label} : {left} pièce(s) en trop introuvables")
    return qty_by_file, remove_by_file, new_lots, actions


def reconcile(folder, cat, fix=True, set_no=None, log=print, backup=True,
              multiplier=None):
    """Check the set and (when `fix`) write the corrected quantities. Returns the report,
    with 'actions' (what was done or would be done) and 'fixed' (True when files changed).

    Must run BEFORE build_plan: it can add or drop lots, and the plan addresses remarks by
    row index."""
    if fix:
        from .plan import set_number as _sn
        normalize_inventory(folder, multiplier if multiplier is not None
                            else set_multiplier(set_no or _sn(folder)), log=log)
    report = check(folder, cat, set_no, multiplier)
    qty_by_file, remove_by_file, new_lots, actions = _plan_fix(report)
    report["actions"] = actions
    report["fixed"] = False
    if not report["diffs"]:
        return report
    if not fix or not report["can_fix"]:
        if report["blocked"]:
            log(f"⚠ Vérification {report['set']} : {report['blocked']}")
        return report

    touched = sorted(set(qty_by_file) | set(remove_by_file))
    if new_lots and report["inventory_path"]:
        touched.append(report["inventory_path"])
    if backup and touched:
        from .apply import backup_files
        backup_files(folder, sorted(set(touched)))

    for path in sorted(set(qty_by_file) | set(remove_by_file)):
        bsx.adjust_items(path, qty_by_row=qty_by_file.get(path),
                         remove_rows=remove_by_file.get(path, ()))
    if new_lots:
        if not report["inventory_path"]:
            log("⚠ Vérification : pas de fichier « Inventory… » — pièces manquantes non ajoutées")
        else:
            bsx.append_lots(report["inventory_path"], new_lots)
    report["fixed"] = True
    return report


def before_apply(folder, cat=None, log=print, backup=True, set_no=None):
    """The pre-Apply pass: check the files, correct the quantities, log what was done.
    Returns the report, or None when config.VERIFY_ON_APPLY is off.

    MUST run before build_plan — correcting can add or drop lots, and the plan addresses its
    remarks by row index. `report["fixed"]` tells the caller the plan has to be rebuilt."""
    if not getattr(config, "VERIFY_ON_APPLY", True):
        return None
    if cat is None:
        from . import catalogdb
        cat = catalogdb.shared()      # one blob parse per process, shared with restants
    try:
        report = reconcile(folder, cat, fix=True, set_no=set_no, log=log, backup=backup)
    except KeyError as e:                 # set absent from the catalog — never block Apply
        log(f"⚠ Vérification ignorée : {e}")
        return None
    log_report(report, log)
    return report


def log_report(report, log=print):
    """One compact block: the totals, then a line per correction."""
    n = len(report["diffs"])
    if not n:
        log(f"✔ Vérification {report['set']} : {report['expected_lots']} lots, "
            f"{report['expected_qty']} pièces — aucun écart.")
        return
    head = "✔ Corrigé" if report.get("fixed") else "⚠ Écarts"
    log(f"{head} — vérification {report['set']} : {n} lot(s) en écart sur "
        f"{report['expected_lots']} ({report['gap_qty']} pièce(s)).")
    for line in report.get("actions", []):
        log("    " + line)
    if report["blocked"] and not report.get("fixed"):
        log(f"    → {report['blocked']}")


def main():
    import argparse
    from .catalogdb import CatalogDB
    from .plan import is_set_folder, set_number
    ap = argparse.ArgumentParser(
        description="Vérifie (et corrige) les quantités d'un set catalogué à la main.")
    ap.add_argument("folder", nargs="?", help="dossier du set ; --all pour tous")
    ap.add_argument("--all", action="store_true", help="tous les sets du dossier CFB")
    ap.add_argument("--fix", action="store_true", help="écrire les corrections")
    ap.add_argument("--no-backup", action="store_true", help="ne pas sauvegarder d'abord")
    args = ap.parse_args()

    folders = []
    if args.all:
        from .plan import iter_set_folders
        folders = iter_set_folders()      # racine + dossiers de batch
    elif args.folder:
        folders = [args.folder]
    else:
        ap.error("donne un dossier de set, ou --all")

    cat = CatalogDB()
    for folder in folders:
        try:
            rep = reconcile(folder, cat, fix=args.fix, backup=not args.no_backup)
            log_report(rep)
        except Exception as e:
            print(f"⚠ {os.path.basename(folder)} : {e}")
    if not args.fix:
        print("\n(lecture seule — relance avec --fix pour écrire)")


if __name__ == "__main__":
    main()
