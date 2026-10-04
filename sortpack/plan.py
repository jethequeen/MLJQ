# -*- coding: utf-8 -*-
"""
Turn a set folder full of numbered bag files into a per-lot remark plan.

Phase A  - assign every distinct part to a final "Sac" bag (~600 g, sorted by
           Category -> Description -> Color).
Phase B  - walk the numbered bags in order; for parts that span several bags,
           stage them in consolidation bins (C1, C2, ...) and, on the last bag,
           emit the "-> Sac n" finalize action. Parts living in a single bag go
           straight to their Sac.
"""

import os
import re
import glob
import bisect
from collections import defaultdict

from . import config
from . import bsx


# --- Remark label helpers ----------------------------------------------------

def _pad(x):
    return f"{int(x):0{config.LABEL_PAD}d}"

def _sac(n):
    return config.SAC_LABEL.format(n=_pad(n))

def _cbin(k, new=False):
    return (config.CBIN_NEW if new else config.CBIN_ADD).format(k=_pad(k))

def _bbin(k, new=False):
    return (config.BBIN_NEW if new else config.BBIN_ADD).format(k=_pad(k))

def _chain(*segs):
    return config.REMARK_ARROW.join(segs)

def _stage(label):
    """A single-step staging label carries the STAGE_MARK so multi-step (arrow) rows sort
    ahead of it in BrickStore's Remarks sort (see config STAGE_MARK)."""
    return label + config.STAGE_MARK

def _color_hint(color_names):
    """' (Red, Blue)' for the colours ALREADY in a D box, or '' if the hint is disabled."""
    if not config.COLOR_HINT_FMT or not color_names:
        return ""
    names = config.COLOR_HINT_SEP.join(sorted(color_names))
    return config.COLOR_HINT_FMT.format(colors=names)


def _sibling_hint(color_names):
    """' + (Black, Red)' — the same mould's OTHER colours arriving in this very bag, the ones
    to combine with before moving on. Trails the whole remark (after any STAGE_MARK) so it
    never disturbs the C / D / Sac sort, and so the '+' always reads the same way."""
    if not getattr(config, "SIBLING_HINT_FMT", "") or not color_names:
        return ""
    names = config.COLOR_HINT_SEP.join(sorted(color_names))
    return config.SIBLING_HINT_FMT.format(colors=names)

def _bin_capacity(n_items):
    """OBSOLETE — la capacite fixe (ceil(n / MAX_CONSOLIDATION_BOXES)) remplissait les boites
    dans l'ordre, donc les premieres restaient les plus chargees tout du long. _Bins equilibre
    desormais sur la population courante. Conserve pour les appels externes eventuels."""
    boxes = max(1, int(config.MAX_CONSOLIDATION_BOXES))
    return max(1, -(-int(n_items) // boxes)) if n_items else 1   # ceil(n/boxes)


def mould_key(part_key_tuple):
    """The 'mould' a part belongs to = (ItemID, Condition), colour-independent.
    All colours of one mould are grouped together (same Sac, shared mould-bag B)."""
    item_id, _color_id, condition = part_key_tuple
    return (item_id, condition)


# --- Set-folder helpers ------------------------------------------------------

def set_number(set_folder):
    """'76342' from '76342 - Spider-Man ...'; sanitized folder name if no digits."""
    name = os.path.basename(set_folder.rstrip("\\/"))
    m = re.match(r"\s*(\d+)", name)
    return m.group(1) if m else re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def is_set_folder(name):
    """A set folder's name starts with the set number (a digit). Everything else in
    CATALOGUAGE_ROOT (backups, CFB output files, desktop.ini, …) is not a set."""
    return bool(re.match(r"\s*\d", name))


def batch_root():
    """Le dossier qui contient les batchs (CFB/Batchs par défaut)."""
    return getattr(config, "BATCH_ROOT", os.path.join(config.CATALOGUAGE_ROOT, "Batchs"))


def iter_set_folders(root=None):
    """Tous les dossiers de sets, où qu'ils soient : à la racine du cataloguage, et dans les
    dossiers de batch (CFB/Batchs/<nom>/<set>).

    Un set rejoint un batch en y étant DÉPLACÉ — le batch possède ses sets. Toute passe qui
    ne balayait que la racine les perdrait de vue : la liste du GUI, l'auto-apply, la
    vérification, l'aide au tri du téléphone. C'est donc ici, et seulement ici, qu'on décide
    où vivent les sets."""
    root = root or config.CATALOGUAGE_ROOT
    out = []
    if not os.path.isdir(root):
        return out

    def _is_set(p, name):
        """Un dossier de set porte un nom qui commence par un chiffre ET contient au moins un
        .bsx. Sans cette seconde condition, une coquille vide — un dossier que Windows a
        refusé de supprimer après un déplacement, ou créé d'avance — apparaîtrait dans la
        liste et partirait en vérification."""
        if not (os.path.isdir(p) and is_set_folder(name)):
            return False
        try:
            return any(n.lower().endswith(".bsx") for n in os.listdir(p))
        except OSError:
            return False

    br = os.path.abspath(batch_root())
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.abspath(p) != br and _is_set(p, name):
            out.append(p)
    if os.path.isdir(br):
        for b in sorted(os.listdir(br)):
            bp = os.path.join(br, b)
            if not os.path.isdir(bp):
                continue
            for name in sorted(os.listdir(bp)):
                p = os.path.join(bp, name)
                if _is_set(p, name):
                    out.append(p)
    return out


# --- File discovery & bag ordering ------------------------------------------

def bag_sort_key(filename):
    """
    Map a bag filename to a sortable key so files order 1, 1-1, 2, 2-1, ...
    Handles '1', '1.bsx', '1 # 1', '1-1', and Drive duplicate markers like '3 (1)'.
    Non-numeric names sort last, alphabetically.
    """
    name = filename[:-4] if filename.lower().endswith(".bsx") else filename
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)          # drop " (2)" duplicate marker
    m = re.match(r"^\s*(\d+)\s*(?:[#-]\s*(\d+))?", name)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else 0
        return (0, main, sub, "")
    return (1, 0, 0, name.lower())


def find_inventory(set_folder):
    """The set's leftovers inventory file (Inventory*.bsx), or None. This is the file
    the numbered-bag discovery SKIPS (config.SKIP_PREFIXES); the restants job writes into
    it and the CFB finaliser folds it into the master."""
    cands = sorted(glob.glob(os.path.join(set_folder, "Inventory*.bsx")))
    return cands[0] if cands else None


def discover_bags(set_folder):
    """Return [(bag_label, path, items)] for the numbered bags, in bag order."""
    files = []
    for path in glob.glob(os.path.join(set_folder, "*.bsx")):
        base = os.path.basename(path)
        if any(base.startswith(p) for p in config.SKIP_PREFIXES):
            continue
        items = bsx.read_items(path)
        if not items:
            continue
        files.append((base[:-4], path, items))
    files.sort(key=lambda t: bag_sort_key(t[0]))
    return files


# --- The plan ---------------------------------------------------------------

def part_key(item):
    return (item["item_id"], item["color_id"], item["condition"])


def _sac_label_re():
    """Finds a written "Sac NN" inside a remark. Derived from config.SAC_LABEL so the
    label stays tunable in one place."""
    return re.compile(re.escape(config.SAC_LABEL).replace(re.escape("{n}"), r"(\d+)"))


def existing_sacs(bags):
    """{part key -> Sac number} as ALREADY WRITTEN in the bags' remarks.

    The remarks are the set's own record of its Sac layout, and once sorting has started
    they outrank a fresh weight flow: a lot the sorter has already dropped in Sac 03 must go
    on reading Sac 03. A staging-only row ("C01") carries no Sac, but the finalize row in a
    later bag does, so scanning every bag finds each key's Sac. The LAST "Sac NN" in a chain
    is the destination — a colour-group hint or the divider's dashes can follow it."""
    rx = _sac_label_re()
    out = {}
    for _label, _path, items in bags:
        for it in items:
            found = rx.findall(it.get("remarks") or "")
            if found:
                out[part_key(it)] = int(found[-1])
    return out


_CBIN_RE = re.compile(r"\bC(\d+)")
_DBIN_RE = re.compile(r"\bD(\d+)")


def existing_labels(bags):
    """What the bags' remarks already say, so a re-plan can keep saying it.

    Returns a dict with:
      sac        {key: Sac number}            — as existing_sacs()
      cbin       {key: C box number}          — the box that colour's small bag is IN
      dbin       {mould: D box number}        — the box that mould's colour-bag is IN
      terminal   {key: Sac number}            — occurrences already sent to their Sac, in a
                                                bag EARLIER than the key's last one

    The box NUMBERS matter as much as the Sacs. They are allocated during the walk from a
    capacity that depends on how many lots consolidate in the whole set, so adding one lot can
    renumber boxes everywhere — including in bags already sorted, where a small bag is
    physically sitting in box C02 while the new plan would start calling it C01. Pinning them
    keeps every already-written label true and keeps later bags pointing at the real box.

    `terminal` is the other half: a colour whose pieces already went to their Sac in an
    earlier bag is DONE, whatever arrives later. Staging it in a C box after the fact would
    tell the sorter to park pieces that are already bagged, and to empty a box that was never
    started — so those occurrences go straight to the Sac instead."""
    sac_rx = _sac_label_re()
    sac, cbin, dbin, term_at, last_at = {}, {}, {}, {}, {}
    for bi, (_label, _path, items) in enumerate(bags):
        for it in items:
            k = part_key(it)
            txt = it.get("remarks") or ""
            last_at[k] = max(last_at.get(k, bi), bi)
            found = sac_rx.findall(txt)
            if found:
                sac[k] = int(found[-1])
                # the EARLIEST bag that already sends it to its Sac: a properly staged
                # spanning colour only reaches its Sac in its LAST bag, so a terminal row
                # anywhere before that means those pieces were bagged and are done.
                term_at[k] = min(term_at.get(k, bi), bi)
            m = _CBIN_RE.search(txt)
            if m and k not in cbin:
                cbin[k] = int(m.group(1))
            m = _DBIN_RE.search(txt)
            if m:
                dbin.setdefault(mould_key(k), int(m.group(1)))
    terminal = {k: sac[k] for k, b in term_at.items() if b < last_at.get(k, b)}
    # (b is the earliest terminal bag; comparing it to the key's last bag stays true once the
    # later occurrence has been written with its Sac too, which is when a max() would fail.)
    return {"sac": sac, "cbin": cbin, "dbin": dbin, "terminal": terminal}


def pinned_sac_of(pinned, meta):
    """Sac per key when the layout is PINNED: every already-written Sac is kept exactly, and
    the new lots slot in beside the parts they belong with — a new colour of a mould joins
    its sisters' Sac (a mould is always packed in one Sac), otherwise the lot takes the Sac of
    its nearest neighbour in the plan's own sort order (type → category → name → colour), the
    lots it would have been packed beside. Nothing already written moves, so bagging already
    done still stands."""
    sac_of = {k: n for k, n in pinned.items() if k in meta}
    mould_sac = {}
    for k, n in sac_of.items():
        mould_sac.setdefault(mould_key(k), n)
    order = sorted(sac_of, key=lambda kk: item_sort_tuple(meta[kk]))
    sortkeys = [item_sort_tuple(meta[kk]) for kk in order]
    sacs = [sac_of[kk] for kk in order]
    for k in meta:
        if k in sac_of:
            continue
        m = mould_key(k)
        if m in mould_sac:                      # keep the mould in one Sac
            sac_of[k] = mould_sac[m]
            continue
        i = bisect.bisect_right(sortkeys, item_sort_tuple(meta[k]))
        sac_of[k] = sacs[0] if i == 0 else sacs[i - 1]
    return sac_of


def flow_sacs(meta, total_qty, weight_g, pinned=None):
    """Phase A on ONE union of lots: {part key -> Sac number} and how many Sacs.

    Extracted from build_plan because the same flow has to serve a plan PREVISIONNEL built
    from the catalog (sortpack/forecast.py). The numbering must come out identical whether
    the lots were read from catalogued .bsx or from the catalog itself -- otherwise the
    forecast would not be worth pinning a real set to.

    `pinned` ({key: Sac}, already written or forecast) wins: nothing already placed moves and
    the new lots slot in beside the parts they belong with (see pinned_sac_of).
    """
    pinned = {k: n for k, n in (pinned or {}).items() if k in meta}
    if pinned:
        sac_of = pinned_sac_of(pinned, meta)
        return sac_of, (max(sac_of.values()) if sac_of else 0)

    def lot_w(k):
        return weight_g.get(k, 0.0) * total_qty.get(k, 0)

    # Group the sorted parts by mould so a multi-colour mould is placed as one atomic
    # block: it opens a fresh Sac rather than being split, UNLESS the block alone is
    # heavier than a Sac (the niche case) -- then it splits like ordinary lots.
    mould_order = {}
    for k in sorted(meta, key=lambda kk: item_sort_tuple(meta[kk])):
        mould_order.setdefault(mould_key(k), []).append(k)

    sac_of = {}
    sac = 1
    running = 0.0
    for keys in mould_order.values():
        if len(keys) >= 2:
            gw = sum(lot_w(k) for k in keys)
            if gw <= config.SAC_TARGET_GRAMS:
                if running > 0 and running + gw > config.SAC_TARGET_GRAMS:
                    sac += 1
                    running = 0.0
                for k in keys:
                    sac_of[k] = sac
                running += gw
                continue
            # else: mould heavier than a whole Sac -> fall through and split it
        for k in keys:
            w = lot_w(k)
            if running > 0 and running + w > config.SAC_TARGET_GRAMS:
                sac += 1
                running = 0.0
            sac_of[k] = sac
            running += w
    return sac_of, (sac if meta else 0)


def type_rank(type_id):
    """Sortable key for an item type, per config.ITEM_TYPE_ORDER (minifig before part,
    ...). Types not listed there sort after the listed ones, by their own id."""
    try:
        return f"{config.ITEM_TYPE_ORDER.index(type_id):02d}"
    except ValueError:
        return f"{len(config.ITEM_TYPE_ORDER):02d}{type_id.lower()}"


_NUM_CHUNK_RE = re.compile(r"(\d+)")


def natural_key(text):
    """« Plate 1 x 3 » avant « Plate 1 x 12 » : les nombres se comparent comme des NOMBRES.

    Le tri de la Phase A décide de quels lots se retrouvent côte à côte, donc dans le même
    Sac, et le trieur longe ses bacs dans cet ordre-là. En texte brut, « plate 1 x 12 »
    passe avant « plate 1 x 3 » — le '1' de 12 bat le '3' au troisième caractère — ce qui
    envoyait la 1x12 au Sac 06 pendant que les 1x3 et 1x5 allaient au Sac 07. Un moule
    rangé à l'envers de sa taille, donc un aller-retour dans les bacs.

    On découpe sur les suites de chiffres et on compare les nombres en nombres. Le découpage
    alterne toujours texte/chiffres (`re.split` sur un groupe capturant rend d'abord un
    morceau non numérique, même vide), donc deux clés ne confrontent jamais un int à une
    str au même rang. Sans chiffres, ça se réduit à la comparaison de texte d'avant."""
    return tuple(int(c) if c.isdigit() else c
                 for c in _NUM_CHUNK_RE.split((text or "").lower()))


def item_sort_tuple(item):
    """The Phase-A sort key for one item, per config.SORT_KEYS."""
    vals = {"type": type_rank(item["item_type"]), "category": item["category_name"],
            "name": item["item_name"], "color": item["color_name"]}
    return tuple(natural_key(vals[s]) for s in config.SORT_KEYS)


class _Bins:
    """Consolidation boxes, balanced on what they hold RIGHT NOW.

    A box is physical and holds several small bags at once; the number on a remark names the
    box, not the bag. What matters is therefore how many small bags are open in each box at
    any moment, and that is what this balances.

    It used to fill by capacity — `ceil(total / MAX_BOXES)` slots each, lowest box first —
    which put the first twelve colours of a 72-colour set in C01, the next twelve in C02, and
    so on. Since colours are met in walk order, the early boxes stayed the busiest for the
    whole pass: you kept rummaging in C01 while C05 sat nearly empty. Allocating to the box
    with the FEWEST bags open right now spreads the live load evenly instead, and a box that
    empties (its colours finished) becomes the next one chosen — a rolling window rather than
    a fixed partition."""

    def __init__(self, max_bins):
        self.max_bins = max(1, int(max_bins))
        self.active = []  # active[i] = number of in-progress groups in bin (i+1)

    def alloc(self):
        """Reserve and return the least-loaded bin, opening a new one while we are still
        under max_bins. Ties go to the lowest number, so a small set still uses C01, C02, …
        in order and never looks scattered."""
        if len(self.active) < self.max_bins:
            self.active.append(1)
            return len(self.active)
        i = min(range(len(self.active)), key=lambda j: (self.active[j], j))
        self.active[i] += 1
        return i + 1

    def take(self, k):
        """Reserve a SPECIFIC bin number, whatever the capacity says. Used when a label is
        pinned: the small bag is physically in that box already, so capacity has to yield."""
        while len(self.active) < k:
            self.active.append(0)
        self.active[k - 1] += 1
        return k

    def free(self, k):
        if 1 <= k <= len(self.active):
            self.active[k - 1] -= 1


def _mult_status(multiplier, file_state, mult_note):
    """Human-readable summary of what the ×Quantité step will do, shown in Preview."""
    if mult_note:
        return mult_note
    if multiplier <= 1:
        return "×1 (aucune multiplication)"
    states = set(file_state.values())
    if states == {"base"}:
        return f"sera multiplié ×{multiplier} à l'Apply"
    if "base" not in states:
        how = "marqueur" if "stamped" in states else "quantités déjà divisibles"
        return f"déjà multiplié ×{multiplier} — détecté ({how}), ne sera PAS re-multiplié"
    return (f"MIXTE : certains sacs déjà ×{multiplier}, d'autres non — "
            f"seuls les non-multipliés seront multipliés")


def set_multiplier_of(folder):
    """How many copies of that set we own (the ×N factor), plus why if we don't know.
    Returns (multiplier, note, ok, error)."""
    if not config.CFB_MULTIPLY_FROM_SHEET:
        return 1, None, True, None
    from . import history
    h = history.HistoryDB()
    try:
        qty = h.inventory_qty(set_number(folder))
    finally:
        h.close()
    if qty and qty > 0:
        return qty, None, True, None
    return 1, ("⚠ aucun achat enregistré pour ce set (table inventory) — "
               "logue l'achat dans le Journal (Catégorie A), sinon calcul à ×1"), False, "not_found"


def build_plan(set_folder, wdb, pin_sacs=False, open_sacs=False, pinned_layout=None,
               pending_keys=None):
    """
    Returns a dict:
      bags: [(label, path, items)]
      remarks: {path: {row: text}}
      stats: summary numbers

    `set_folder` is ONE set folder, or a LIST of them — a BATCH. A batch is planned exactly
    like a single set, on the union of its lots: same sort, same 650 g, same C/D lifecycle,
    one Sac numbering across the whole shipment. That is the point — the customer unpacks it
    in one pass. Each set keeps its own ×N multiplier (they are different purchases), which
    is why the factor is tracked per FILE and not per plan.

    pin_sacs=True keeps the Sac layout ALREADY WRITTEN in the bags instead of flowing it
    again from the weights (see existing_sacs / pinned_sac_of). Use it for every re-plan that
    happens while the set is being sorted — placing a leftover, moving a lot forward — so a
    lot already dropped in a Sac is never renumbered under the sorter. A fresh Apply plans
    from scratch (pin_sacs=False), which is when the weights decide.

    pinned_layout={key: Sac} is the same idea one step EARLIER: the plan previsionnel a
    batch computed from the catalog (sortpack/forecast.py) before its other sets arrived, so
    the set in hand is sorted against the numbering the whole shipment will have. What the
    bags already SAY still outranks it -- that is physical reality, a forecast is not.

    pending_keys = les (piece, couleur) qu'un set du batch PAS ENCORE CATALOGUE apportera
    encore. Sans elles, Phase B ne voit que les sacs presents et vide sa boite C au dernier
    sac du set en main — alors que la meme piece, meme couleur, arrive dans un set qui n'est
    pas la. Phase A, elle, le savait deja (le plan previsionnel couvre tout l'envoi) : c'est
    cette asymetrie que le parametre corrige. Une couleur annoncee comme « encore a venir »
    ne finit jamais dans ce passage, donc elle reste en C jusqu'a ce que le set manquant soit
    catalogue a son tour.

    open_sacs=True is how a BATCH is sorted: its Sacs are known from the first lot and stay
    open on the table, so a finished colour goes straight into its Sac. That removes the D
    box entirely — Phase A already puts every colour of a mould in the SAME Sac, so they
    converge there on their own, and parking them in a D bag first would mean hunting for
    that bag on every finished colour for no gain. The C box stays: a colour still being
    collected has to be gathered somewhere, or we would ship two lots of the same
    (part, colour). The grouping the customer wants — one sub-bag per mould — is done at the
    end by the pass in batch.consolidation_pass, which never looks outside one Sac.
    """
    folders = [set_folder] if isinstance(set_folder, str) else list(set_folder)

    # Per-set quantity multiplier = how many copies we own, read from our SQLite
    # `inventory` table (populated from the Journal's Catégorie-A purchases — see
    # sortpack/purchases.py). Bags already stamped (multiplied on a previous Apply) keep
    # a factor of 1 so we never scale twice; unstamped bags get the full multiplier.
    bags, file_mult, file_state, path_mult = [], {}, {}, {}
    mults, notes, mult_ok, mult_error = {}, [], True, None
    for folder in folders:
        m, note, ok, err = set_multiplier_of(folder)
        mults[folder] = m
        if not ok:
            mult_ok = False
            mult_error = err
            notes.append(f"{set_number(folder)} : {note}")
        for label, path, items in discover_bags(folder):
            bags.append((label, path, items))
            _, raw = bsx.read_item_blocks(path)
            st = bsx.multiplication_state(raw, m)
            file_state[path] = st
            file_mult[path] = m if st == "base" else 1
            path_mult[path] = m          # le xN du SET auquel ce fichier appartient
    multiplier = mults[folders[0]] if len(folders) == 1 else max(mults.values(), default=1)
    mult_note = "\n".join(notes) if notes else None

    # Aggregate across bags (quantities scaled by each file's effective multiplier).
    total_qty = defaultdict(int)
    meta = {}                        # key -> item (for category/name/color/weight)
    bags_with = defaultdict(set)     # key -> set of bag indices
    for bi, (_, path, items) in enumerate(bags):
        fm = file_mult[path]
        for it in items:
            k = part_key(it)
            total_qty[k] += it["qty"] * fm
            bags_with[k].add(bi)
            meta.setdefault(k, it)

    # A lot can reach a file without its <CategoryName> — BrickStore fills that in from its
    # own catalog when it opens the file, and anything we append before that has none. The
    # category is a property of the mould, so borrow it from another lot of the same item
    # rather than letting the blank sort the lot ahead of everything (Phase A sorts on it).
    cat_by_item = {}
    for k in meta:
        if meta[k].get("category_name"):
            cat_by_item.setdefault(k[0], meta[k]["category_name"])
    for k in meta:
        if not meta[k].get("category_name") and k[0] in cat_by_item:
            meta[k] = dict(meta[k], category_name=cat_by_item[k[0]])

    # Weights.
    weight_g = {k: (wdb.weight(k[0], meta[k]["item_name"]) or 0.0) for k in meta}

    # Mould = (ItemID, Condition); a mould in >=2 colours is "multi-colour": its colours
    # are grouped in a mould-bag (B) and forced into the same Sac.
    mould_colors = defaultdict(set)
    mould_color_names = defaultdict(set)     # mould -> {colour NAMES} for the D-row hint
    for k in meta:
        mould_colors[mould_key(k)].add(k[1])
        mould_color_names[mould_key(k)].add(meta[k]["color_name"])
    is_multi = {k: len(mould_colors[mould_key(k)]) >= 2 for k in meta}

    # --- Phase A: Sac assignment ---
    # A set already carrying remarks has a Sac layout the sorter is working to; when we are
    # asked to pin it, that layout wins and only the new lots get placed. The weights then
    # decide nothing, so a Sac can end up over target -- that is the point: a piece added to
    # a bag that was already full makes it heavier, it does not renumber the shelf. A
    # forecast (pinned_layout) sits UNDER that: the bags' own remarks overrule it.
    labels = existing_labels(bags) if pin_sacs else {"sac": {}, "cbin": {}, "dbin": {},
                                                      "terminal": {}}
    pinned = {k: n for k, n in (pinned_layout or {}).items() if k in meta}
    pinned.update({k: n for k, n in labels["sac"].items() if k in meta})
    # Colours already sent to their Sac in an earlier bag: done, whatever turns up later.
    straight = {k for k in labels["terminal"] if k in meta}
    sac_of, num_sacs = flow_sacs(meta, total_qty, weight_g, pinned=pinned)

    # --- Phase B: walk bags in order, emit remarks (C -> B -> Sac lifecycle) ---
    #   C: a colour still being collected across bags (not finished).
    #   B: a finished colour of a multi-colour mould, waiting in the mould-bag for its
    #      sister colours; the mould-bag ships to its Sac once every colour is finished.
    color_last = {k: max(bags_with[k]) for k in bags_with}
    # A colour already bagged in an earlier pass never "spans": its extra pieces join the Sac
    # its first ones are already in, rather than being staged in a box after the fact.
    spans = {k: (len(bags_with[k]) > 1 and k not in straight) for k in meta}

    # Les couleurs qu'un set non catalogue apportera encore ne FINISSENT PAS ici : on leur
    # donne un « dernier sac » qu'aucun indice n'atteint, donc chaque occurrence retombe dans
    # la mise en boite C. Sauf celles qui sont deja parties au Sac (`straight`) : on ne
    # dessache pas ce qui est deja en sac, les pieces suivantes les y rejoindront.
    for k in (pending_keys or ()):
        if k in meta and k not in straight:
            spans[k] = True
            color_last[k] = len(bags)        # hors de portee de bi (0..len(bags)-1)

    # A multi-colour mould only needs a D BOX when its colours FINISH in DIFFERENT bags.
    # A D box exists to park a colour that is DONE while its sister colours are still
    # coming, so what matters is where each colour FINISHES — not every bag it passes
    # through. A mould whose colours all complete in the SAME bag has nothing to park, even
    # when some of those colours were collected across several bags: they arrive from their
    # C boxes in that very bag, so the whole mould is complete there and ships straight to
    # its shared Sac as an adjacent GROUP row. (Using every bag the mould appeared in used
    # to send a colour present in one bag only through a pointless D box, and made its row
    # read like its sisters' — the sorter was told to park a mould that was already done.)
    mould_fin_bags = defaultdict(set)
    for k in meta:
        if k not in straight:          # it is not waiting for anybody; it is already packed
            mould_fin_bags[mould_key(k)].add(color_last[k])
    if open_sacs:
        # The Sacs are open: a finished colour goes straight into its own, and its sisters
        # land in the same one. Nothing to park, so no D box at all.
        needs_dbox = {m: False for m in mould_colors}
    else:
        needs_dbox = {m: (len(mould_colors[m]) >= 2 and len(mould_fin_bags[m]) >= 2)
                      for m in mould_colors}

    # For a D-box mould, all its colours FINISHING in the same bag are handled as one GROUP
    # (identical remark -> BrickStore sorts them adjacent, so you grab them together). Every
    # D row lists the mould's full box contents THROUGH that bag, so you can spot the bag in
    # the box. The box opens at the mould's first finish bag and empties at its last.
    mould_finish = defaultdict(lambda: defaultdict(list))   # mould -> bag -> [colour names]
    for k in meta:
        mk = mould_key(k)
        if k not in straight and needs_dbox[mk]:
            mould_finish[mk][color_last[k]].append(meta[k]["color_name"])
    # Every mould's colours by the bag they FINISH in — not just the D-box ones. Several
    # colours of one mould completing in the same bag have to be COMBINED there (into the D
    # box, or straight into their shared Sac), and that is the fact the remark has to carry:
    # each row names its siblings, so the sorter knows to go find them.
    mould_fin_colors = defaultdict(lambda: defaultdict(list))   # mould -> bag -> [colours]
    for k in meta:
        if k not in straight:
            mould_fin_colors[mould_key(k)][color_last[k]].append(meta[k]["color_name"])

    def _siblings_now(k, bag_i):
        """The mould's other colours finishing in `bag_i` — what to combine this lot with."""
        own = meta[k]["color_name"]
        return sorted({c for c in mould_fin_colors[mould_key(k)].get(bag_i, []) if c != own})

    mould_first_fin = {m: min(fin) for m, fin in mould_finish.items()}
    mould_last_fin = {m: max(fin) for m, fin in mould_finish.items()}
    d_open = defaultdict(list)      # bag -> moulds whose D-box opens here
    d_close = defaultdict(list)     # bag -> moulds whose D-box empties here
    for m in mould_finish:
        d_open[mould_first_fin[m]].append(m)
        d_close[mould_last_fin[m]].append(m)

    def _d_contents(m, upto_bag):
        """All colour names in mould m's D-bag through `upto_bag` (its group so far)."""
        names = []
        for b, cols in mould_finish[m].items():
            if b <= upto_bag:
                names.extend(cols)
        return names

    # Dynamic box capacities (33% rule): the C boxes are sized to the number of colours
    # that span >1 bag, the D boxes to the number of moulds that actually use a D box, so
    # each type uses at most MAX_CONSOLIDATION_BOXES boxes holding ~1/Nth of the lots.
    n_span_colours = sum(1 for k in meta if spans[k])
    n_dbox_moulds = sum(1 for v in needs_dbox.values() if v)
    # Les boites sont equilibrees sur ce qu'elles contiennent a l'instant t (voir _Bins),
    # pas decoupees d'avance : le nombre de boites est le plafond, pas une part fixe.
    cbins = _Bins(config.MAX_CONSOLIDATION_BOXES)
    bbins = _Bins(config.MAX_CONSOLIDATION_BOXES)
    # Box numbers already written are reserved before anything else is handed out, so a
    # re-plan keeps calling each physical box by the name on it.
    pin_cbin = {k: n for k, n in labels["cbin"].items() if k in meta and spans.get(k)}
    pin_dbin = {m: n for m, n in labels["dbin"].items()}
    for _k, _n in sorted(pin_cbin.items(), key=lambda kv: kv[1]):
        cbins.take(_n)
    cbin = {}          # part key -> C bin number (while its colour is being collected)
    cbin_open = {}     # part key -> the bag its C bin was opened in (never "conso" there)
    bbin = {}          # mould    -> D bin number (while its colours are being grouped)
    completed = {}     # key -> its final remark, so duplicate rows of a key reuse it
    remarks = defaultdict(dict)

    for bi, (_, path, items) in enumerate(bags):
        for m in d_open.get(bi, []):       # each mould's D-box opens at its first finish bag
            bbin[m] = bbins.take(pin_dbin[m]) if m in pin_dbin else bbins.alloc()
        for it in items:
            k = part_key(it)
            row = it["row"]

            # Already bagged in an earlier pass: every occurrence just joins that Sac. No
            # C box (its pieces are not waiting anywhere) and no D box (its grouping is done).
            if k in straight:
                remarks[path][row] = completed.setdefault(k, _sac(sac_of[k]))
                continue

            # Colour still being collected in later bags -> stage in a C bin (single-step).
            if spans[k] and bi != color_last[k]:
                if k not in cbin:                    # first bag of THIS colour = new bin
                    cbin[k] = pin_cbin.get(k) or cbins.alloc()
                    cbin_open[k] = bi
                # Several lots of the SAME colour in ONE bag are one act of staging, so they
                # must read alike: "conso" starts at the colour's SECOND bag (a colour is
                # never "conso" in the bag where its small bag is started).
                remarks[path][row] = _stage(_cbin(cbin[k], new=(cbin_open[k] == bi)))
                continue

            # Colour is finished at this occurrence.
            if k in completed:                 # duplicate row of an already-finished key
                remarks[path][row] = completed[k]
                continue

            m = mould_key(k)
            segs = []
            staging_dbin = False               # a single-step D-box staging row (needs the mark)
            if k in cbin:                      # it was staged in a C bin (created earlier)
                segs.append(_cbin(cbin[k], new=False))   # -> empty the EXISTING C bin
                cbins.free(cbin[k])

            if needs_dbox[m]:                  # cross-bag multi-colour mould -> D box
                # GROUP: every colour of this mould finishing in THIS bag shares one remark
                # (identical -> sorts adjacent), listing what is ALREADY in the box from
                # EARLIER bags (before you add this bag's colours) so you can spot the bag.
                # The mould's FIRST bag has nothing there yet, so it's a bare "D01" (you're
                # starting the box, not consolidating into it).
                prior = _d_contents(m, bi - 1)              # colours from strictly earlier bags
                if prior:
                    dseg = _bbin(bbin[m], new=False) + _color_hint(prior)   # "D01 conso (…)"
                else:
                    dseg = _bbin(bbin[m], new=True)         # "D01"  (first bag: nothing there)
                segs.append(dseg)
                if bi == mould_last_fin[m]:    # last finish bag -> empty the box into its Sac
                    segs.append(_sac(sac_of[k]))
                else:
                    staging_dbin = (len(segs) == 1)   # "D01…" (not "C.. -> D01…")
            elif is_multi[k]:                  # same-bag multi mould -> GROUP straight to Sac
                segs.append(_sac(sac_of[k]))
            else:                              # single-colour mould -> straight to its Sac
                segs.append(_sac(sac_of[k]))

            remark = _chain(*segs)
            # A single-step D-box staging row takes the STAGE_MARK (appended last) so multi-
            # step finalize rows sort ahead of it; terminal "Sac" rows never do.
            if staging_dbin:
                remark = _stage(remark)
            # … and last of all, the colours arriving in THIS bag with it. Whether they are
            # going into a D box or straight to the shared Sac, they must end up in one bag,
            # and the rows can read differently (one of them may be coming out of a C box),
            # so each one has to name the others instead of relying on sorting adjacent.
            if is_multi[k]:
                remark += _sibling_hint(_siblings_now(k, bi))

            completed[k] = remark
            remarks[path][row] = completed[k]
        for m in d_close.get(bi, []):      # free the box after its last finish bag
            bbins.free(bbin[m])

    # Sac dividers (point 1): in each bag file, turn ONE plain "Sac n" row per Sac into
    # the SAC_MARK divider so that after a click-sort on Remarks each Sac block ends with
    # a visible line before the next Sac. Only plain-Sac rows qualify (the B*/C* rows sort
    # into their own block at the top, so they're never dividers).
    if config.SAC_MARK:
        plain_to_sac = {_sac(n): n for n in set(sac_of.values())}
        for path, rows in remarks.items():
            seen = set()
            for row in sorted(rows):
                n = plain_to_sac.get(rows[row])
                if n is not None and n not in seen:
                    seen.add(n)
                    rows[row] = config.SAC_MARK.format(n=_pad(n))

    sac_grams = defaultdict(float)
    for k in meta:
        sac_grams[sac_of[k]] += weight_g[k] * total_qty[k]

    stats = {
        "bags": len(bags),
        "open_sacs": bool(open_sacs),
        "pinned_sacs": len(pinned),
        "pinned_bins": len(pin_cbin) + len(pin_dbin),
        "already_bagged": len(straight),
        "sac_grams": {n: round(g, 1) for n, g in sorted(sac_grams.items())},
        "overweight_sacs": sorted(n for n, g in sac_grams.items()
                                  if g > config.SAC_TARGET_GRAMS),
        "unique_parts": len(meta),
        "consolidated_parts": sum(1 for k in meta if spans[k]),
        "cbins_used": len(cbins.active),
        "color_grouped_parts": sum(1 for k in meta if is_multi[k]),
        "bbins_used": len(bbins.active),
        "multi_colour_moulds": sum(1 for c in mould_colors.values() if len(c) >= 2),
        "sacs": num_sacs,
        "total_weight_g": sum(weight_g[k] * total_qty[k] for k in meta),
        "missing_weight": [k for k in meta if not weight_g[k]],
        "multiplier": multiplier,
        "multipliers": {set_number(f): mults[f] for f in folders},
        "folders": list(folders),
        "mult_note": mult_note,
        "mult_ok": mult_ok,
        "mult_error": mult_error,
        "mult_status": _mult_status(multiplier, file_state, mult_note),
    }
    return {"bags": bags, "remarks": remarks, "stats": stats,
            "sac_of": sac_of, "meta": meta, "weight_g": weight_g,
            "total_qty": total_qty, "multiplier": multiplier,
            "file_mult": file_mult, "file_state": file_state, "path_mult": path_mult,
            "mould_colors": mould_colors, "bags_with": bags_with,
            "is_multi": is_multi, "spans": spans, "color_last": color_last,
            "mould_color_names": mould_color_names, "needs_dbox": needs_dbox,
            "cbin": cbin, "bbin": bbin}
