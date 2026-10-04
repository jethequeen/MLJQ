# -*- coding: utf-8 -*-
"""
Point 3 — "Calculer les restants".

After sorting the numbered bags, the user marks a lot's <Remarks> with "x" when it is
missing / uncertain in that bag — a stand-in for cutting it out of the bag file and
pasting it into the leftovers inventory. This module performs that move and tells the
user where each physical leftover goes:

  1. MOVE every "x"-marked lot out of the numbered bags and INTO the set's `Inventory*`
     file (merging quantities by (ItemID, ColorID, Condition)).
  2. Reuse the numbered-bag plan's Sac assignments ("les Sacs déjà en cours") — leftovers
     slot into the SAME Sacs, not new ones.
  3. Write each inventory lot's <Remarks> with its destination Sac, plus a warning when
     the lot belongs to a consolidation (`C -> Sac 06`) or a colour group (`D -> Sac 06`).
     At this final stage the physical C/D bins have been reused, so we give the action
     type + the Sac, not a bin number.

The processed inventory file is then packed last, like a normal bag; the CFB finaliser
folds its present lots into the master (see finalize.build_cfb_files).
"""

import os
import re
import json
import bisect
import datetime
from math import gcd
from collections import defaultdict
from xml.sax.saxutils import escape

from . import config
from . import bsx
from . import apply as apply_mod
from .plan import (build_plan, find_inventory, mould_key, set_number, _sac, _pad,
                   _chain, _color_hint, item_sort_tuple as _sort_tuple)


def _part_key(it):
    return (it["item_id"], it["color_id"], it["condition"])


def plan_for(set_folder, wdb, pin_sacs=True):
    """Le plan qui fait autorite pour ce set.

    Un set qui appartient a un batch ne se planifie PAS seul : sa numerotation de Sacs court
    sur tout l'envoi, une couleur peut traverser deux sets, et le batch se trie sacs ouverts
    (donc sans boite D). On replanifie donc le batch entier, ce qui est aussi ce qu'il faut
    pour reecrire les remarques apres un deplacement de lot.

    Si le batch a un PLAN PREVISIONNEL (sortpack/forecast.py), il est epingle : les sets
    arrives portent alors les numeros de Sacs de l'envoi COMPLET, et l'aide au tri du
    telephone dit la meme chose que les remarques ecrites dans les fichiers.

    Hors batch, rien ne change : le set est planifie comme avant."""
    try:
        from . import batch as _batch
        # FERME COMPRIS : un envoi ferme garde ses sets jusqu'a `finish()`, et c'est
        # justement une fois ferme qu'on le trie. Avec `batch_of` seul, le telephone
        # retombait sur un plan par set des la fermeture.
        name = _batch.owning_batch(set_number(set_folder))
        if name:
            fs, layout = _batch.plan_inputs(name)
            return build_plan(fs, wdb, pin_sacs=pin_sacs, pinned_layout=layout,
                              pending_keys=_batch.pending_keys(name),
                              open_sacs=getattr(config, "BATCH_OPEN_SACS", True))
    except Exception:
        pass                       # registre illisible / dossier manquant : on reste par set
    return build_plan(set_folder, wdb, pin_sacs=pin_sacs)


def _currency(inv_path, bags):
    """Currency of the inventory file, else the first bag's, else CAD."""
    for p in ([inv_path] if inv_path else []) + [b[1] for b in bags]:
        try:
            _, raw = bsx.read_item_blocks(p)
        except OSError:
            continue
        m = re.search(r'<Inventory[^>]*\bCurrency="([^"]+)"', raw)
        if m:
            return m.group(1)
    return "CAD"


def _emit_item(block, qty, remark):
    """Re-emit one <Item> block with a new Qty and Remarks, canonical indentation."""
    block = re.sub(r"<Remarks>.*?</Remarks>", "", block, flags=re.DOTALL)
    block = re.sub(r"<Qty>\s*\d+\s*</Qty>", f"<Qty>{qty}</Qty>", block, count=1)
    esc = escape(remark)
    out = []
    for ln in (l.strip() for l in block.splitlines()):
        if not ln:
            continue
        if ln.startswith("</Item>"):
            out.append("   <Remarks>%s</Remarks>" % esc)
        pad = "  " if ln.startswith("<Item>") or ln.startswith("</Item>") else "   "
        out.append(pad + ln)
    return "\n".join(out)


def _document(currency, item_texts, multiplier_stamp=None):
    """Rebuild an inventory document. `multiplier_stamp` re-emits the MLJQ-xN marker: the
    file is rewritten from scratch here, and dropping the marker would leave the quantities
    looking un-multiplied to the next Apply."""
    open_tag = f' <Inventory Currency="{currency}">'
    if multiplier_stamp:
        open_tag += f"<!--MLJQ-xN={int(multiplier_stamp)}-->"
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<BrickStoreXML>", open_tag]
    lines.extend(item_texts)
    lines += [" </Inventory>", "</BrickStoreXML>", ""]
    return "\n".join(lines)


def sac_locator(plan):
    """A function key -> destination Sac, for ANY part of the set.

    A part that never made it into a numbered bag has no Sac of its own, so it inherits its
    mould's Sac if a sister colour has one (a mould is always packed in one Sac), else the Sac
    of its nearest neighbour in the plan's own order (category -> name -> colour) — the parts
    it would have been packed beside. Same rule as plan.pinned_sac_of, so what the phone
    predicts is what a placement actually writes."""
    sac_of, meta = plan["sac_of"], plan["meta"]
    keys = sorted(sac_of, key=lambda kk: _sort_tuple(meta[kk]))
    sortkeys = [_sort_tuple(meta[kk]) for kk in keys]
    sacs = [sac_of[kk] for kk in keys]
    mould_sac = {}
    for kk in keys:
        mould_sac.setdefault(mould_key(kk), sac_of[kk])

    def dest(k, item):
        if k in sac_of:
            return sac_of[k]
        if not sacs:
            return 1
        m = mould_key(k)
        if m in mould_sac:
            return mould_sac[m]
        i = bisect.bisect_right(sortkeys, _sort_tuple(item))
        return sacs[0] if i == 0 else sacs[i - 1]

    return dest


def _catalogue_done(set_folder):
    """Le cataloguage de ce set est-il fini ? (meme reponse que partout ailleurs). On
    repond True si on ne sait pas : c'est l'ancien comportement, et l'aide au tri ne doit
    jamais se vider sur un doute."""
    try:
        from . import remote as _remote
        return _remote._catalogue_done(set_folder)
    except Exception:
        return True


def bag_help(set_folder, wdb=None, plan=None, cat=None, include_catalog=None):
    """Everything the phone needs for « je suis au sac N et j'ai cette pièce en main ».

    The answer depends on where you are in the walk, because a part can only go straight to
    its Sac once nothing more of it is coming: if the same colour turns up in a LATER bag it
    has to wait in a C box, and if a sister colour of the same mould does, it waits in the
    mould's D box so the colours reach the Sac together. So rather than a remark per (part,
    bag) pair, this hands the phone the two facts it needs to decide for any bag — the bags
    that still hold this exact part, and those that hold its sister colours — plus the Sac,
    which never moves.

    Quantities are PHYSICAL (×N, the whole purchase), matching the files after Apply.

    `parts` also carries the set's catalogue parts that no file lists any more (absent=True),
    so a lot you deleted from a bag can be put back when the piece turns up. Pass
    include_catalog=False (or config.BAG_HELP_INCLUDE_CATALOG) to skip reading the catalog."""
    if include_catalog is None:
        include_catalog = getattr(config, "BAG_HELP_INCLUDE_CATALOG", True)
    if plan is None:
        if wdb is None:
            from .weightdb import WeightDB
            wdb = WeightDB()
        # pinned: the phone must answer with the Sacs the bags actually carry, not with a
        # layout a fresh weight flow would now prefer.
        plan = plan_for(set_folder, wdb)

    meta, bags_with = plan["meta"], plan["bags_with"]
    total_qty, mould_colors = plan["total_qty"], plan["mould_colors"]
    dest_sac = sac_locator(plan)
    labels = [label for label, _p, _i in plan["bags"]]

    # Le plan d'un ENVOI marche sur les sacs de plusieurs sets, et leurs étiquettes se
    # ressemblent : il y a un « 1 » par set. Deux conséquences pour le téléphone, qui
    # raisonne en numéros de sac DU SET qu'il affiche :
    #
    #   * `bag_index` : la pastille N de CE set → l'indice du dernier de ses fichiers de
    #     sac N dans la marche de l'envoi. C'est ce qu'il faut comparer pour répondre « il
    #     en vient encore après ? ». Sans lui, la page cherchait la dernière étiquette qui
    #     commence par N — et tombait sur le sac 1 de l'AUTRE set ;
    #   * `bag_owner` : à qui appartient chaque sac, pour que « garde-la jusqu'au sac 3 »
    #     dise de quel set il s'agit quand ce n'est pas celui qu'on a sous les yeux.
    here = os.path.normcase(os.path.abspath(set_folder))
    bag_owner, bag_index = [], {}
    for i, (label, path, _items) in enumerate(plan["bags"]):
        owner = set_number(os.path.dirname(path))
        bag_owner.append(owner)
        if os.path.normcase(os.path.abspath(os.path.dirname(path))) != here:
            continue
        m = re.match(r"\s*(\d+)", label)
        if m:
            bag_index[str(int(m.group(1)))] = i      # le DERNIER fichier de ce sac gagne

    # mould -> {colour id -> bag indices}, so we can tell a part's own bags from its sisters'
    mould_members = defaultdict(list)
    for k in meta:
        mould_members[mould_key(k)].append(k)

    def sisters_bags(k):
        out = set()
        for kk in mould_members.get(mould_key(k), ()):
            if kk[1] != k[1]:                      # another colour of the same mould
                out |= bags_with.get(kk, set())
        return sorted(out)

    def color_names(k):
        names = {meta[kk]["color_name"] for kk in mould_members.get(mould_key(k), ())
                 if meta[kk].get("color_name")}
        return sorted(n for n in names if n)

    # The boxes the plan actually assigned, so the phone can name them: "boîte C02" /
    # "boîte D03" rather than a bare "boîte C" / "boîte D". A colour only has a C box when
    # the plan sees it in several bags, and a mould only has a D box when its colours finish
    # in different bags — so a leftover the plan never had to stage has no number yet. It
    # gets one the moment it is placed in the bag being sorted (the re-plan assigns it), and
    # place_in_bag hands back the remark that names it.
    plan_cbin = plan.get("cbin") or {}
    plan_bbin = plan.get("bbin") or {}
    plan_needs_d = plan.get("needs_dbox") or {}

    def cbox(k):
        return plan_cbin.get(k)

    def dbox(k):
        m = mould_key(k)
        return plan_bbin.get(m) if plan_needs_d.get(m) else None

    # Cataloguage pas fini : le fichier d'inventaire n'est PAS une liste de restants, c'est
    # ce qui reste a cataloguer. On le DIT au telephone (`cataloguing`), qui n'en deroule
    # alors pas la liste entiere — plusieurs centaines de lots qu'on n'a pas en main n'ont
    # rien d'une liste de choses a faire. Mais on l'envoie quand meme en entier, parce que
    # c'est par la qu'on RATTRAPE une erreur : une piece mal saisie se retrouve, se cherche
    # et se place dans le sac ou on est, exactement comme un vrai restant. La vider ici
    # rendait ces lots introuvables — ni dans les restants, ni dans la recherche.
    cataloguing = not _catalogue_done(set_folder)

    restants = []
    inv_path = find_inventory(set_folder)
    # The quantities shown are always PHYSICAL (the whole purchase). The inventory file is
    # written at base and brought to ×N by normalize_inventory at Apply, so scale it here
    # only while it is still at base — never twice.
    mult = plan.get("multiplier", 1) or 1
    inv_scale = 1
    if inv_path and mult > 1:
        with open(inv_path, "r", encoding="utf-8") as f:
            if bsx.multiplication_state(f.read(), mult) == "base":
                inv_scale = mult
    inv_keys = set()
    if inv_path:
        for it in bsx.read_items(inv_path):
            k = _part_key(it)
            inv_keys.add(f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}')
            restants.append({
                "key": f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}',
                "id": it["item_id"], "color": it["color_name"],
                "color_id": it["color_id"], "name": it["item_name"],
                "type": it["item_type"],
                "qty": it["qty"] * inv_scale, "file_qty": it["qty"],
                "remark": (it["remarks"] or "").strip(),
                "sac": dest_sac(k, it),
                "self": sorted(bags_with.get(k, set())),
                "sisters": sisters_bags(k),
                "colors": color_names(k) if len(mould_colors[mould_key(k)]) >= 2 else [],
                "cbin": cbox(k), "dbin": dbox(k),
            })

    # every part of the set, so « elle n'est pas dans les restants » can still be answered
    parts = []
    for k, it in meta.items():
        parts.append({
            "key": f'{k[0]}|{k[1]}|{k[2]}',
            "id": it["item_id"], "color": it["color_name"], "name": it["item_name"],
            "color_id": k[1], "type": it["item_type"],
            "qty": total_qty.get(k, 0), "sac": dest_sac(k, it),
            "bags": sorted(bags_with.get(k, set())),
            "cbin": cbox(k), "dbin": dbox(k), "absent": False,
        })

    # …plus the parts the CATALOGUE says belong to this set that NO file lists any more. A lot
    # deleted from a bag (because it was not physically there) vanishes from the phone
    # entirely otherwise — not in the leftovers, not in the search — so when the piece finally
    # turns up there is no way to put it back. These carry absent=True and the quantity still
    # owed, and restants.add_from_catalog recreates the lot in the bag being sorted.
    if include_catalog:
        try:
            known = _catalog_parts(set_folder, cat=cat)
            from . import categories
            # "absent" means no file holds it — the leftovers count as holding it, since the
            # restants list already offers those (« Fait »).
            # `inv_keys` plutot que les seuls restants : pendant le cataloguage la liste de
            # restants est vide alors que le fichier d'inventaire tient bel et bien ces
            # pieces. Sans ca, tout ce qui reste a cataloguer ressortirait en « absent »,
            # avec un bouton pour le recreer dans le sac en cours.
            held = {p["key"] for p in parts} | {r["key"] for r in restants} | inv_keys
            for (item_id, color_id), (pt, base_qty) in known.items():
                if f"{item_id}|{color_id}|N" in held:
                    continue
                item = {"item_type": pt.item_type, "item_name": getattr(pt, "item_name", ""),
                        "color_name": pt.color_name,
                        "category_name": categories.category_of(item_id)[1]}
                parts.append({
                    "key": f"{item_id}|{color_id}|N",
                    "id": item_id, "color": pt.color_name,
                    "name": getattr(pt, "item_name", ""),
                    "color_id": color_id, "type": pt.item_type,
                    "qty": base_qty * (plan.get("multiplier", 1) or 1),
                    "sac": dest_sac((item_id, color_id, "N"), item),
                    "bags": [], "cbin": None, "dbin": None, "absent": True,
                })
        except Exception:
            pass          # no catalog on this machine -> the old behaviour, never a failure
    parts.sort(key=lambda p: (p["name"] or "", p["color"] or ""))

    return {"set": set_number(set_folder),
            "name": os.path.basename(set_folder.rstrip("\\/")),
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "multiplier": plan.get("multiplier", 1),
            "bags": labels, "bag_owner": bag_owner, "bag_index": bag_index,
            "restants": restants, "parts": parts,
            "cataloguing": cataloguing,
            "inv_scale": inv_scale}


def write_bag_help(set_folder, wdb=None, plan=None):
    """Write bag_help() beside the set's other phone files. Best-effort: returns the path,
    or None when it couldn't be produced (a Drive hiccup must never break an Apply)."""
    try:
        data = bag_help(set_folder, wdb=wdb, plan=plan)
        path = os.path.join(config.CATALOGUAGE_ROOT,
                            config.BAG_HELP_FILE.format(set=data["set"]))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return path
    except Exception:
        return None


class _SrcLot(object):
    """Just enough of a catalog Part for bsx._item_block, taken from an existing lot.

    The CATEGORY comes along deliberately: the planner sorts by category, so a lot that
    landed in a bag file without one would sort ahead of everything and be packed into the
    first Sac instead of beside its own kind."""
    __slots__ = ("item_id", "item_type", "color_id", "color_name", "item_name", "qty",
                 "category_id", "category_name")

    def __init__(self, it, qty):
        self.item_id, self.item_type = it["item_id"], it["item_type"]
        self.color_id, self.color_name = it["color_id"], it["color_name"]
        self.item_name, self.qty = it["item_name"], qty
        self.category_id = it.get("category_id", "")
        self.category_name = it.get("category_name", "")


class _CatLot(object):
    """Just enough of a Part for bsx._item_block, for a lot rebuilt from the CATALOG.

    The catalog blob reader does not decode categories, and Phase A sorts on the category —
    so it is filled from sortpack.categories (harvested from the .bsx files we already own),
    otherwise the lot would sort ahead of everything and land in the first Sac."""
    __slots__ = ("item_id", "item_type", "color_id", "color_name", "item_name", "qty",
                 "category_id", "category_name")

    def __init__(self, part, qty, condition="N"):
        from . import categories
        self.item_id, self.item_type = part.item_id, part.item_type
        self.color_id, self.color_name = part.color_id, part.color_name
        self.item_name = getattr(part, "item_name", "") or ""
        self.qty = qty
        self.category_id, self.category_name = categories.category_of(part.item_id)


def _catalog_parts(set_folder, cat=None):
    """{(item_id, color_id): (Part, base_qty)} — what BrickStore says the set contains, at
    base quantity for one copy. Extras included, exactly like verify's reference."""
    if cat is None:
        from . import catalogdb
        cat = catalogdb.shared()
    out = {}
    for pt in cat.inventory(set_number(set_folder), include_extras=True):
        k = (str(pt.item_id), str(pt.color_id))
        if k in out:
            out[k] = (out[k][0], out[k][1] + pt.qty)
        else:
            out[k] = (pt, pt.qty)
    return out


def add_from_catalog(set_folder, key, to_bag, qty=None, wdb=None, backup=True, log=None,
                     cat=None):
    """Put a part BACK into the set from the catalog, into the bag you are sorting.

    For the piece that no file lists any more: you deleted the lot from a bag because it was
    not physically in it, and now it has turned up. Nothing in the set holds it, so neither
    « Fait » (which reads the leftovers) nor « La traiter ici » (which drains a later bag) can
    help — the part has to be recreated.

    Unlike every other move this INCREASES what the files hold, which is the whole point, so
    it is bounded by the catalog: the set can never end up holding more of a part than
    BrickStore says it contains (expected base × N). Returns a summary with the new remark.
    """
    from .apply import apply_plan
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()

    plan = plan_for(set_folder, wdb)
    bags = plan["bags"]
    if not bags:
        raise RuntimeError("ce set n'a aucun sac numéroté")
    if not (0 <= int(to_bag) < len(bags)):
        raise RuntimeError(f"sac {to_bag} inconnu dans ce set")
    to_bag = int(to_bag)
    dest_label, dest_path, _ = bags[to_bag]

    parts = key.split("|")
    if len(parts) != 3:
        raise RuntimeError(f"clé de pièce invalide : {key}")
    item_id, color_id, condition = parts[0], parts[1], parts[2]

    known = _catalog_parts(set_folder, cat=cat)
    entry = known.get((item_id, color_id))
    if entry is None:
        raise RuntimeError(f"{item_id} / couleur {color_id} n'appartient pas à ce set "
                           f"d'après le catalogue BrickStore — rien à rajouter")
    part, base_qty = entry

    mult = plan.get("multiplier", 1) or 1
    scales = _scales(set_folder, mult)
    dest_scale = scales.get(dest_path) or 1
    expected_physical = base_qty * mult

    before = _physical_totals(set_folder, scales)
    have_physical = before.get((item_id, color_id, condition), 0)
    room = expected_physical - have_physical
    if room <= 0:
        raise RuntimeError(f"le set contient déjà ses {have_physical} × {item_id} "
                           f"(catalogue : {expected_physical}) — rien à rajouter")

    take_physical = room if qty is None else min(max(1, int(round(float(qty)))), room)
    snapped = None
    if dest_scale > 1 and take_physical % dest_scale:
        snapped = take_physical
        take_physical = min(max(dest_scale, (take_physical // dest_scale) * dest_scale), room)
    if dest_scale > 1 and take_physical % dest_scale:
        raise RuntimeError(f"il reste {room} pièce(s) à rajouter, ce qui n'est pas un nombre "
                           f"entier d'exemplaires (×{dest_scale})")
    take_dest = take_physical // dest_scale

    backup_dir = apply_mod.backup_files(set_folder, [dest_path]) if backup else None
    bsx.ensure_stamp(dest_path, mult)

    dest_rows = [it for it in bsx.read_items(dest_path)
                 if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key)]
    if dest_rows:
        bsx.adjust_items(dest_path,
                         qty_by_row={dest_rows[0]["row"]: dest_rows[0]["qty"] + take_dest})
    else:
        bsx.append_lots(dest_path, [(_CatLot(part, take_dest, condition), take_dest)])

    after = _physical_totals(set_folder, _scales(set_folder, mult))
    grew = after.get((item_id, color_id, condition), 0) - have_physical
    for k, v in before.items():                 # nothing ELSE may move
        if k != (item_id, color_id, condition) and after.get(k) != v:
            raise RuntimeError("ANNULÉ : l'ajout a changé une autre pièce — "
                               f"restaure {backup_dir or 'la sauvegarde'}")
    if grew != take_physical:
        raise RuntimeError(f"ANNULÉ : {take_physical} attendues, {grew} ajoutées — "
                           f"restaure {backup_dir or 'la sauvegarde'}")

    old_remarks = {(pa, r): t for pa, rows in plan["remarks"].items()
                   for r, t in rows.items()}
    new_plan = plan_for(set_folder, wdb)
    apply_plan(set_folder, new_plan, backup=False)
    changed = sum(1 for pa, rows in new_plan["remarks"].items()
                  for row, text in rows.items()
                  if old_remarks.get((pa, row)) != text)
    earlier = _earlier_changes(plan, old_remarks, new_plan, to_bag)

    remark = ""
    for _label, pa, items in new_plan["bags"]:
        if pa != dest_path:
            continue
        for it in items:
            if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key):
                remark = new_plan["remarks"].get(pa, {}).get(it["row"], "")
    write_bag_help(set_folder, plan=new_plan)

    if log:
        log(f"Rajouté {take_physical} × {key} au sac {dest_label} → "
            f"{remark or 'sans remarque'}.")
    return {"key": str(key), "to_bag": to_bag, "to_label": dest_label,
            "added_physical": take_physical, "expected_physical": expected_physical,
            "left_physical": room - take_physical, "snapped_from": snapped,
            "multiplier": mult, "remark": remark, "remarks_changed": changed,
            "earlier_changed": earlier,
            "backup_dir": backup_dir, "name": getattr(part, "item_name", ""),
            "color": getattr(part, "color_name", "")}


def _file_totals(set_folder):
    """Physical pieces per (item, colour, condition) across every .bsx of the set. The one
    invariant a move must never break."""
    out = defaultdict(int)
    for name in sorted(os.listdir(set_folder)):
        if not name.lower().endswith(".bsx"):
            continue
        for it in bsx.read_items(os.path.join(set_folder, name)):
            out[_part_key(it)] += it["qty"]
    return dict(out)


def transfer_to_bag(set_folder, key, to_bag, qty=None, wdb=None, backup=True, log=None):
    """Move a part you are holding NOW into the bag you are sorting, and re-plan.

    You are at bag `to_bag` (a plan bag index) and the part is catalogued in a LATER bag: it
    is already out of the pile, so rather than carrying it forward, the lot moves to where
    you actually are. `qty` is PHYSICAL (default: everything still sitting in later bags).

    Only later bags are drained — an earlier bag is already sorted, and taking from it would
    contradict work you have done. The whole set is then re-planned and the remarks rewritten
    (never re-multiplied), because moving a lot changes the C / D lifecycle of that colour and
    of its mould. Returns a summary, including how many OTHER lots' remarks moved with it.
    """
    from .apply import apply_plan
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()

    plan = plan_for(set_folder, wdb)   # never renumber a bagged Sac
    bags = plan["bags"]
    if not (0 <= int(to_bag) < len(bags)):
        raise RuntimeError(f"sac {to_bag} inconnu dans ce set")
    to_bag = int(to_bag)
    dest_label, dest_path, _ = bags[to_bag]

    # what we can take: lots of this part sitting in LATER bags, nearest first
    sources = []
    for bi in range(to_bag + 1, len(bags)):
        label, path, items = bags[bi]
        for it in items:
            if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key) and it["qty"] > 0:
                sources.append((bi, label, path, it))
    if not sources:
        raise RuntimeError("cette pièce n'est listée dans aucun sac suivant — "
                           "rien à déplacer (elle est déjà passée, ou elle est en restants)")
    available = sum(it["qty"] for _bi, _l, _p, it in sources)
    mult0 = plan.get("multiplier", 1) or 1
    snapped = None
    if qty is None:
        take = available                      # the whole lot: always a whole number of copies
    else:
        take = min(int(qty), available)
        # You own N copies, so a lot is "so many PER COPY". Moving a count that is not a
        # multiple of N would leave both files holding a fraction of a copy - the quantities
        # would still add up, but nothing downstream could reason per copy again. So snap
        # down to whole copies (and never below one copy's worth).
        if mult0 > 1 and take % mult0:
            snapped = take
            take = max(mult0, (take // mult0) * mult0)
            take = min(take, available)
    if take <= 0:
        raise RuntimeError("quantité à déplacer nulle")

    before = _file_totals(set_folder)
    touched = {dest_path} | {p for _bi, _l, p, _it in sources}
    backup_dir = apply_mod.backup_files(set_folder, sorted(touched)) if backup else None

    # A file BrickStore saved has lost its ×N marker and is only recognised as multiplied
    # because every quantity divides by N. Moving a non-multiple (13 pieces, say) would
    # destroy that evidence and the next Apply would multiply the file AGAIN, so pin the
    # state down before touching anything.
    mult = plan.get("multiplier", 1) or 1
    for path in sorted(touched):
        bsx.ensure_stamp(path, mult)

    # --- take it out of the later bags ---
    taken_from, left = [], take
    by_path = defaultdict(lambda: ({}, []))          # path -> (qty_by_row, remove_rows)
    src_item = None
    for _bi, label, path, it in sources:
        if left <= 0:
            break
        cut = min(it["qty"], left)
        left -= cut
        src_item = src_item or it
        q, rem = by_path[path]
        if it["qty"] - cut <= 0:
            rem.append(it["row"])
        else:
            q[it["row"]] = it["qty"] - cut
        taken_from.append(f"{cut} du sac {label}")
    for path, (q, rem) in by_path.items():
        bsx.adjust_items(path, qty_by_row=q, remove_rows=rem)

    # --- put it into the bag being sorted ---
    dest_rows = [it for it in bsx.read_items(dest_path)
                 if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key)]
    if dest_rows:
        bsx.adjust_items(dest_path, qty_by_row={dest_rows[0]["row"]: dest_rows[0]["qty"] + take})
    else:
        bsx.append_lots(dest_path, [(_SrcLot(src_item, take), take)])

    after = _file_totals(set_folder)
    if after != before:                       # must never happen: a move creates nothing
        raise RuntimeError("ANNULÉ : le déplacement aurait changé les quantités — "
                           f"restaure {backup_dir or 'la sauvegarde'}")

    # --- re-plan: the colour's C/D lifecycle depends on which bags hold it ---
    old_remarks = {(p, r): t for p, rows in plan["remarks"].items() for r, t in rows.items()}
    new_plan = plan_for(set_folder, wdb)
    apply_plan(set_folder, new_plan, backup=False)    # remarks only; the ×N stamp guards itself

    changed = 0
    for path, rows in new_plan["remarks"].items():
        for row, text in rows.items():
            if old_remarks.get((path, row)) != text:
                changed += 1
    earlier = _earlier_changes(plan, old_remarks, new_plan, to_bag)

    dest_remark = ""
    for label, path, items in new_plan["bags"]:
        if path != dest_path:
            continue
        for it in items:
            if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key):
                dest_remark = new_plan["remarks"].get(path, {}).get(it["row"], "")
    write_bag_help(set_folder, plan=new_plan)

    if log:
        log(f"Déplacé {take} × {key} → sac {dest_label} ({', '.join(taken_from)}).")
    return {"key": str(key), "qty": take, "to_bag": to_bag, "to_label": dest_label,
            "from": taken_from, "remark": dest_remark, "remarks_changed": changed,
            "earlier_changed": earlier,
            "snapped_from": snapped, "multiplier": mult0, "backup_dir": backup_dir}


def _earlier_changes(plan, old_remarks, new_plan, to_bag):
    """Labels that changed in bags BEFORE the one being sorted — work already done.

    Sacs are pinned and box numbers are pinned, so this is normally empty. What can still
    move is a staging label whose premise changed: a colour parked in box D01 at bag 3
    because it was finished there is no longer finished once you add more of it at bag 8, and
    its bag-3 row becomes a C row. The pieces are still physically in D01, so the row is
    retrospectively wrong and nobody can tell from the file — hence this is reported rather
    than swallowed, with the bag labels to go and look at."""
    labels = {}
    for bi, (label, path, _items) in enumerate(new_plan["bags"]):
        if bi >= to_bag:
            continue
        for row, text in (new_plan["remarks"].get(path) or {}).items():
            if old_remarks.get((path, row)) not in (None, text):
                labels.setdefault(label, 0)
                labels[label] += 1
    return labels


def _scale_of(path, mult):
    """The factor a file's quantities still have to be multiplied by to become PHYSICAL:
    `mult` while the file is at base, 1 once it is already ×N. Same rule build_plan uses
    for `file_mult`, but it also covers the inventory (which build_plan never walks).

    The bags and the inventory are routinely on DIFFERENT footings — Apply multiplies the
    bags, while the leftovers are written at base — so anything moving between them has to
    convert."""
    if not mult or mult <= 1:
        return 1
    with open(path, "r", encoding="utf-8") as f:
        return mult if bsx.multiplication_state(f.read(), mult) == "base" else 1


def _scales(set_folder, mult):
    """{path: factor} for every .bsx of the set, read ONCE — reused for the before/after
    check so the invariant is measured on the same footing on both sides."""
    out = {}
    for name in sorted(os.listdir(set_folder)):
        if name.lower().endswith(".bsx"):
            path = os.path.join(set_folder, name)
            out[path] = _scale_of(path, mult)
    return out


def _physical_totals(set_folder, scales):
    """Physical pieces per (item, colour, condition) across every .bsx, each file counted at
    its own ×N state. The one invariant a move must never break — and the only one that
    means anything when the bags are already multiplied and the inventory is not."""
    out = defaultdict(int)
    for path, scale in scales.items():
        if not os.path.exists(path):
            continue
        for it in bsx.read_items(path):
            out[_part_key(it)] += it["qty"] * scale
    return dict(out)


def place_in_bag(set_folder, key, to_bag, qty=None, wdb=None, backup=True, log=None):
    """« Fait » — the leftover is in your hand at bag `to_bag`, so it JOINS that bag.

    Taking it off the inventory alone would lose the pieces: no other file of the set holds
    them, so the CFB master would ship short. They belong to the bag you are sorting, which
    is also what makes the answer honest — the set is re-planned with the part in THIS bag,
    so its remark names the box it waits in (`C02`, `D03 conso (…)`) or the Sac it goes
    straight to, and every other lot whose C / D lifecycle that changed is rewritten with it.

    `key` is "<itemid>|<colorid>|<condition>" as bag_help emits it, `to_bag` a plan bag
    index (what bag_help's `bags` list is indexed by) and `qty` a PHYSICAL count — leave it
    None to place the whole lot. Returns a summary, including the new remark.
    """
    from .apply import apply_plan
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()

    inv_path = find_inventory(set_folder)
    if inv_path is None:
        raise RuntimeError("aucun fichier « Inventory… » dans ce set")

    # Pinned on both sides: the Sacs already written are the ones the sorter is working to,
    # so placing a lot must never renumber them (only the new lot gets a Sac).
    plan = plan_for(set_folder, wdb)
    bags = plan["bags"]
    if not bags:
        raise RuntimeError("ce set n'a aucun sac numéroté")
    if not (0 <= int(to_bag) < len(bags)):
        raise RuntimeError(f"sac {to_bag} inconnu dans ce set")
    to_bag = int(to_bag)
    dest_label, dest_path, _ = bags[to_bag]

    rows = [it for it in bsx.read_items(inv_path)
            if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key)]
    if not rows:
        raise RuntimeError(f"pièce {key} absente de l'inventaire (déjà faite ?)")

    # The two files are often on different ×N footings (Apply multiplies the bags; the
    # leftovers are written at base), so the move is done in PHYSICAL pieces and converted
    # per file. Every file's state is read once, before anything is edited.
    mult = plan.get("multiplier", 1) or 1
    scales = _scales(set_folder, mult)
    inv_scale = scales.get(inv_path) or 1
    dest_scale = scales.get(dest_path) or 1

    have_physical = sum(it["qty"] for it in rows) * inv_scale
    take_physical = have_physical if qty is None \
        else min(max(1, int(round(float(qty)))), have_physical)
    # A quantity has to come out whole in BOTH files, so it must be a multiple of each one's
    # remaining ×N factor — of their lcm. You own N copies, so that is just "a whole number
    # of copies": a fraction of one would leave both files holding a fraction of a copy that
    # nothing downstream could reason about.
    step = inv_scale * dest_scale // gcd(inv_scale, dest_scale)
    snapped = None
    if step > 1 and take_physical % step:
        snapped = take_physical                       # snap to whole copies, at least one
        take_physical = min(max(step, (take_physical // step) * step), have_physical)
    if step > 1 and take_physical % step:             # the lot itself is a fraction of a copy
        raise RuntimeError(u"ce lot (%d pièces) n'est pas un nombre entier d'exemplaires "
                           u"(×%d) — lance un « Appliquer » pour remettre les quantités "
                           u"d'aplomb" % (have_physical, step))
    take_inv = take_physical // inv_scale
    take_dest = take_physical // dest_scale

    before = _physical_totals(set_folder, scales)
    touched = sorted({inv_path, dest_path})
    backup_dir = apply_mod.backup_files(set_folder, touched) if backup else None
    for path in touched:                 # keep the ×N evidence a partial take would erase
        bsx.ensure_stamp(path, mult)

    # --- take it off the inventory ---
    qty_by_row, remove_rows, left = {}, [], take_inv
    for it in rows:
        if left <= 0:
            break
        cut = min(it["qty"], left)
        left -= cut
        if it["qty"] - cut <= 0:
            remove_rows.append(it["row"])
        else:
            qty_by_row[it["row"]] = it["qty"] - cut
    bsx.adjust_items(inv_path, qty_by_row=qty_by_row, remove_rows=remove_rows)

    # --- put it in the bag being sorted ---
    dest_rows = [it for it in bsx.read_items(dest_path)
                 if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key)]
    if dest_rows:
        bsx.adjust_items(dest_path,
                         qty_by_row={dest_rows[0]["row"]: dest_rows[0]["qty"] + take_dest})
    else:
        bsx.append_lots(dest_path, [(_SrcLot(rows[0], take_dest), take_dest)])

    after = _physical_totals(set_folder, scales)
    if after != before:                  # must never happen: a move creates nothing
        raise RuntimeError("ANNULÉ : le déplacement aurait changé les quantités — "
                           f"restaure {backup_dir or 'la sauvegarde'}")

    # --- re-plan: the part now lives in this bag, which moves its C / D lifecycle ---
    old_remarks = {(p, r): t for p, rows_ in plan["remarks"].items()
                   for r, t in rows_.items()}
    new_plan = plan_for(set_folder, wdb)
    apply_plan(set_folder, new_plan, backup=False)     # remarks only; the ×N stamp guards itself

    # apply_plan also brings the leftovers to ×N (normalize_inventory), which rewrites the
    # very quantities we just touched — so count the pieces once more, on freshly read
    # scales. Nothing here may create or lose a piece.
    if _physical_totals(set_folder, _scales(set_folder, mult)) != before:
        raise RuntimeError("ALERTE : le compte de pièces a changé après le déplacement — "
                           f"vérifie le set et restaure {backup_dir or 'la sauvegarde'}")

    changed = sum(1 for path, rows_ in new_plan["remarks"].items()
                  for row, text in rows_.items()
                  if old_remarks.get((path, row)) != text)
    earlier = _earlier_changes(plan, old_remarks, new_plan, to_bag)

    remark = ""
    for _label, path, items in new_plan["bags"]:
        if path != dest_path:
            continue
        for it in items:
            if f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}' == str(key):
                remark = new_plan["remarks"].get(path, {}).get(it["row"], "")
    write_bag_help(set_folder, plan=new_plan)          # the phone's list just changed

    if log:
        log(f"Restant {key} : {take_physical} placée(s) dans le sac {dest_label} "
            f"→ {remark or 'sans remarque'}.")
    return {"key": str(key), "to_bag": to_bag, "to_label": dest_label,
            "placed_physical": take_physical,
            "left_physical": have_physical - take_physical,
            "cleared": have_physical == take_physical,
            "snapped_from": snapped, "multiplier": mult,
            "remark": remark, "remarks_changed": changed, "earlier_changed": earlier,
            "backup_dir": backup_dir}


def mark_done(set_folder, key, to_bag=None, qty=None, wdb=None, backup=True, log=None):
    """« Fait » — kept as the name the phone's `restant_done` maps to; it places the
    leftover in the bag being sorted (see place_in_bag). `to_bag` is required: without a
    destination the pieces would simply vanish from the set's files, so we refuse rather
    than lose them."""
    if to_bag is None:
        raise RuntimeError("sac de destination manquant — le restant doit être placé dans "
                           "le sac en cours de tri (mets l'appli du téléphone à jour)")
    return place_in_bag(set_folder, key, to_bag, qty=qty, wdb=wdb, backup=backup, log=log)


def compute_restants(set_folder, wdb, backup=True):
    """
    Move the 'x'-marked lots into the inventory and (re)write the inventory's remarks so
    each leftover shows where to pack it. Returns a summary dict. Backs up every file it
    touches (bags with x + the inventory) first, unless backup=False.
    """
    inv_path = find_inventory(set_folder)
    if inv_path is None:
        raise RuntimeError(
            "Aucun fichier « Inventory… » dans ce dossier de set — impossible de calculer "
            "les restants. (Le fichier des pièces non cataloguées doit exister.)")

    # Stable Sac assignments: build the plan on the bags AS-IS (x lots still present) and
    # PINNED to the Sacs already written on them — those are the Sacs being filled.
    plan = plan_for(set_folder, wdb)
    bags = plan["bags"]
    # Per-bag quantity multiplier (= how many copies of the set we own, or 1 if that bag is
    # already scaled). A leftover moved into the inventory must carry the SAME ×qty as the
    # bags do, so we scale each moved lot by its source bag's factor (never double-scaling).
    file_mult = plan.get("file_mult", {})

    # --- 1. Gather the 'x'-marked lots per bag ---
    mark = config.CFB_MISSING_REMARK.strip().casefold()
    moved = {}          # key -> total qty moved
    moved_block = {}    # key -> a representative raw <Item> block
    moved_item = {}     # key -> a representative parsed item (for sort/category)
    xrows_by_path = {}
    for _label, path, items in bags:
        blocks, _raw = bsx.read_item_blocks(path)
        xrows = [it["row"] for it in items
                 if it["remarks"].strip().casefold() == mark]
        if not xrows:
            continue
        xrows_by_path[path] = set(xrows)
        fm = file_mult.get(path, 1)
        for it in items:
            if it["row"] in xrows_by_path[path]:
                k = _part_key(it)
                moved[k] = moved.get(k, 0) + it["qty"] * fm     # scale by the set ×qty
                moved_block.setdefault(k, blocks[it["row"]])
                moved_item.setdefault(k, it)

    # --- Back up everything we are about to touch ---
    touched = list(xrows_by_path.keys()) + [inv_path]
    backup_dir = apply_mod.backup_files(set_folder, touched) if (backup and touched) else None

    # --- 2. Remove the x lots from the bag files (the "cut") ---
    for path, xrows in xrows_by_path.items():
        _blocks, raw = bsx.read_item_blocks(path)
        new_raw = bsx.remove_item_rows(raw, xrows)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_raw)

    # --- 3. Merge the moved lots into the inventory (the "paste") ---
    inv_items = bsx.read_items(inv_path)
    inv_blocks, _ = bsx.read_item_blocks(inv_path)
    merged = {}         # key -> {"qty", "block", "item"}
    order = []
    for it in inv_items:
        k = _part_key(it)
        if k not in merged:
            merged[k] = {"qty": 0, "block": inv_blocks[it["row"]], "item": it}
            order.append(k)
        merged[k]["qty"] += it["qty"]
    for k in moved:
        if k not in merged:
            merged[k] = {"qty": 0, "block": moved_block[k], "item": moved_item[k]}
            order.append(k)
        merged[k]["qty"] += moved[k]

    # --- 4. Destination Sac + C/B warning per leftover, reusing the plan's Sacs ---
    sac_of = plan["sac_of"]
    spans = plan["spans"]
    meta = plan["meta"]

    combined_colors = defaultdict(set)          # mould -> colour IDs (plan + inventory)
    combined_color_names = defaultdict(set)     # mould -> colour NAMES (for the D hint)
    for k in meta:
        combined_colors[mould_key(k)].add(k[1])
        combined_color_names[mould_key(k)].add(meta[k]["color_name"])
    for k in merged:
        combined_colors[mould_key(k)].add(k[1])
        combined_color_names[mould_key(k)].add(merged[k]["item"]["color_name"])
    # A leftover is only sent looking for a D bag when the plan actually opened one for its
    # mould (its colours finish in different bags). When they all finished in the same bag
    # they went straight to their shared Sac, so there is no box to find: the leftover just
    # joins them there, and a "D conso (…)" would send the sorter hunting for nothing.
    plan_needs_d = plan.get("needs_dbox") or {}

    # Leftovers that were never in a numbered bag inherit their neighbour's Sac.
    dest_sac = sac_locator(plan)

    def remark_for(k, item):
        n = dest_sac(k, item)
        segs = []
        if spans.get(k, False):                          # was consolidated across bags
            segs.append(config.RESTANT_CBIN)
        if plan_needs_d.get(mould_key(k)):               # the mould really has a D bag
            # Behave like a D conso: list the mould's colours so you know what to look for
            # when regrouping the leftover with its sisters ("D conso (Green, Red) -> Sac 02").
            segs.append(config.RESTANT_BBIN + " conso"
                        + _color_hint(combined_color_names[mould_key(k)]))
        segs.append(_sac(n))
        return _chain(*segs)

    remarks_list = [remark_for(k, merged[k]["item"]) for k in order]

    # Sac dividers (point 1) on plain-Sac rows, one per Sac.
    if config.SAC_MARK:
        plain_to_sac = {_sac(n): n for n in set(sac_of.values()) | {dest_sac(k, merged[k]["item"]) for k in order}}
        seen = set()
        for i, txt in enumerate(remarks_list):
            n = plain_to_sac.get(txt)
            if n is not None and n not in seen:
                seen.add(n)
                remarks_list[i] = config.SAC_MARK.format(n=_pad(n))

    # --- 5. Write the processed inventory file ---
    currency = _currency(inv_path, bags)
    with open(inv_path, "r", encoding="utf-8") as f:
        inv_stamp = bsx.multiplier_stamp(f.read())
    item_texts = [_emit_item(merged[k]["block"], merged[k]["qty"], remarks_list[i])
                  for i, k in enumerate(order)]
    with open(inv_path, "w", encoding="utf-8", newline="") as f:
        f.write(_document(currency, item_texts, multiplier_stamp=inv_stamp))

    write_bag_help(set_folder, plan=plan)     # the phone's restants list just changed

    return {
        "set_number": set_number(set_folder),
        "inventory_path": inv_path,
        "bags_touched": len(xrows_by_path),
        "moved_lots": len(moved),
        "moved_qty": sum(moved.values()),
        "inventory_lots": len(order),
        "inventory_qty": sum(merged[k]["qty"] for k in order),
        "backup_dir": backup_dir,
    }


def main():
    """`python -m sortpack.restants [dossier|--all]` — (re)write the phone's per-set sorting
    helper. It is produced automatically at Apply; this is for refreshing it by hand."""
    import argparse
    from .plan import iter_set_folders
    from .weightdb import WeightDB
    ap = argparse.ArgumentParser(
        description="Regenere le fichier d'aide au tri lu par le telephone.")
    ap.add_argument("folder", nargs="?", help="dossier du set")
    ap.add_argument("--all", action="store_true", help="tous les sets du dossier CFB")
    args = ap.parse_args()

    folders = []
    if args.all:
        folders = iter_set_folders()
    elif args.folder:
        folders = [args.folder]
    else:
        ap.error("donne un dossier de set, ou --all")

    wdb = WeightDB()
    for folder in folders:
        name = os.path.basename(folder.rstrip("\/"))
        try:
            data = bag_help(folder, wdb=wdb)
            path = write_bag_help(folder, wdb=wdb)
            print(f"  {name[:38]:38} {len(data['restants']):3} restants, "
                  f"{len(data['parts']):4} pieces -> {os.path.basename(path or '?')}")
        except Exception as e:
            print(f"  {name[:38]:38} erreur : {str(e)[:60]}")


if __name__ == "__main__":
    main()
