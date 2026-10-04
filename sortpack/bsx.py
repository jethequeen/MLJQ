# -*- coding: utf-8 -*-
"""
Read/write BrickStore's native .bsx (XML) lists.

Reading uses ElementTree. Writing uses minimal text surgery: we only inject or
replace each Item's <Remarks> element and leave the rest of the file (indentation,
the GuiState CDATA blobs, field order) byte-for-byte intact, so BrickStore reads
the result exactly as it wrote it. Item blocks are matched in document order,
which is the same order ElementTree returns and the same "row" order BrickStore
and the MCP server use.
"""

import os
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import config

_ITEM_RE = re.compile(r"<Item>.*?</Item>", re.DOTALL)
_GUISTATE_RE = re.compile(r"[ \t]*<GuiState\b.*?</GuiState>", re.DOTALL)
_CLOSE_ROOT_RE = re.compile(r"</BrickStoreXML>")
_REMARKS_RE = re.compile(r"<Remarks>.*?</Remarks>", re.DOTALL)
_QTY_RE = re.compile(r"<Qty>\s*(\d+)\s*</Qty>")
_INVENTORY_OPEN_RE = re.compile(r"<Inventory\b[^>]*>")
# Stamped into a file the first time its quantities are multiplied, so a second
# Apply won't multiply again. Cleared naturally when the bags are regenerated x1.
_MARKER_RE = re.compile(r"<!--\s*MLJQ-xN=(\d+)\s*-->")


def multiplier_stamp(raw):
    """Return the multiplier a file was already stamped with, or None if unstamped."""
    m = _MARKER_RE.search(raw)
    return int(m.group(1)) if m else None


def multiplication_state(raw, multiplier):
    """
    Decide whether a bag file has already been multiplied by `multiplier`, so we never
    do it twice. Returns one of:
      'stamped'   — carries a MLJQ-xN marker: definitely already multiplied.
      'divisible' — no marker, but EVERY <Qty> divides evenly by the multiplier. With a
                    real multiplier (say 10) and hundreds of lots this is virtually proof
                    the file is already multiplied (base part counts are never all ÷10).
                    This is the safety net for legacy files that predate the marker, or
                    files whose marker BrickStore stripped on save.
      'base'      — looks un-multiplied: safe to multiply.
    A multiplier of 1 (or 0) is a no-op, always 'base'.
    """
    if multiplier_stamp(raw) is not None:
        return "stamped"
    if multiplier and multiplier > 1:
        qtys = [int(q) for q in _QTY_RE.findall(raw)]
        if qtys and all(q % multiplier == 0 for q in qtys):
            return "divisible"
    return "base"


def read_items(path):
    """Return a list of dicts (one per Item, in document order). row = list index."""
    root = ET.parse(path).getroot()
    items = []
    for row, it in enumerate(root.findall(".//Item")):
        def g(tag):
            v = it.findtext(tag)
            return v if v is not None else ""
        items.append({
            "row": row,
            "item_id": g("ItemID"),
            "item_type": g("ItemTypeID"),
            "color_id": g("ColorID"),
            "item_name": g("ItemName"),
            "color_name": g("ColorName"),
            "category_id": g("CategoryID"),
            "category_name": g("CategoryName"),
            "condition": g("Condition"),
            "qty": int(g("Qty") or 0),
            "remarks": g("Remarks"),
        })
    return items


def read_item_blocks(path):
    """
    Return (blocks, raw): the list of raw <Item>...</Item> text blocks in document
    order (same order/indexing as read_items' rows) and the whole file text. Used by
    the CFB finalizer, which preserves each lot's original block (Price, LotID, …)
    instead of rebuilding it from parsed fields.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return _ITEM_RE.findall(raw), raw


def set_gui_state(raw, gui_state=None):
    """Return `raw` with its <GuiState> block replaced by the known-good column/sort
    layout (config.BSX_GUI_STATE), or that block inserted before </BrickStoreXML> if the
    file has none. A no-op if OVERWRITE_GUI_STATE is off or no layout is configured. The
    blob is generic BrickStore view state (columns + sort/filter), so it transplants
    safely between bag files and makes them open with the right columns already sorted."""
    if not getattr(config, "OVERWRITE_GUI_STATE", False):
        return raw
    block = gui_state if gui_state is not None else getattr(config, "BSX_GUI_STATE", "")
    if not block:
        return raw
    if _GUISTATE_RE.search(raw):
        return _GUISTATE_RE.sub(lambda _m: block, raw, count=1)
    if _CLOSE_ROOT_RE.search(raw):
        return _CLOSE_ROOT_RE.sub(block + "\n</BrickStoreXML>", raw, count=1)
    return raw


def write_remarks(path, remarks_by_row, out_path=None):
    """
    Write remarks (a dict {row: text}) into the Item blocks of `path`. Rows not in
    the dict are left untouched. Writes to out_path (default: in place).
    Returns the number of Item blocks modified.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    state = {"i": -1, "n": 0}

    def repl(m):
        state["i"] += 1
        row = state["i"]
        if row not in remarks_by_row:
            return m.group(0)
        block = m.group(0)
        text = escape(str(remarks_by_row[row]))
        new_el = f"<Remarks>{text}</Remarks>"
        if _REMARKS_RE.search(block):
            block = _REMARKS_RE.sub(new_el, block, count=1)
        else:
            # Insert right before the closing tag, matching the block's indentation.
            indent = re.search(r"\n([ \t]*)<Item>", "\n" + block)
            pad = indent.group(1) + " " if indent else "  "
            block = block.replace("</Item>", f"{pad}<Remarks>{text}</Remarks>\n{pad[:-1]}</Item>")
        state["n"] += 1
        return block

    new_raw = _ITEM_RE.sub(repl, raw)
    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return state["n"]


_REMARKS_NONEMPTY_RE = re.compile(r"<Remarks>\s*\S")


def has_remarks(path):
    """True if the file has at least one non-empty <Remarks> element. Used to tell whether
    a leftovers Inventory file has been through "Calculer les restants" (which stamps each
    leftover's destination into its Remarks) — a fresh/auto-generated inventory has none."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return bool(_REMARKS_NONEMPTY_RE.search(f.read()))
    except OSError:
        return False


# <Remarks>text</Remarks>, <Remarks/> or no element at all — only real text counts as
# stamped (the plainer "<Remarks>\s*\S" test would accept an empty <Remarks></Remarks>,
# since the "<" of the closing tag is itself non-space).
_REMARKS_ANY_RE = re.compile(r"<Remarks\s*/>|<Remarks>(.*?)</Remarks>", re.DOTALL)


def _stamped(item_block):
    """True if this <Item> block carries a remark with actual text in it."""
    m = _REMARKS_ANY_RE.search(item_block)
    return bool(m and (m.group(1) or "").strip())


def remark_coverage(path):
    """(lots, lots_already_stamped) for one file: how many <Item> blocks it has, and how
    many of those carry a non-empty <Remarks>. Text-only (no XML parse) so the auto-apply
    poll can ask it about every bag of every set cheaply. A lot with no <Remarks> element
    at all counts as un-stamped."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return (0, 0)
    blocks = _ITEM_RE.findall(raw)
    return len(blocks), sum(1 for b in blocks if _stamped(b))


def total_qty(path):
    """Fast sum of every <Qty> in a .bsx (regex, no XML parse). 0 if unreadable. Used to
    gauge how 'empty' the leftovers Inventory file is vs the numbered bags."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return 0
    return sum(int(q) for q in _QTY_RE.findall(raw))


# BrickLink ItemTypeID letter → the <ItemTypeName> BrickStore writes. Only used to make a
# generated inventory read naturally; BrickStore re-resolves it from its catalog anyway.
_ITEM_TYPE_NAMES = {"P": "Part", "M": "Minifig", "S": "Set", "B": "Book", "G": "Gear",
                    "C": "Catalog", "I": "Instruction", "O": "Original Box"}


def ensure_stamp(path, multiplier, out_path=None):
    """Pin a file's "already multiplied" state by writing the MLJQ-xN marker.

    A file BrickStore has saved has lost its marker, so the only remaining evidence that it
    is already ×N is that every quantity divides by N ('divisible'). That evidence is
    destroyed the moment anything writes a quantity that is not a multiple — and the next
    Apply would then multiply the whole file a SECOND time. So before editing quantities in
    such a file, nail the state down. Returns True if a marker was added."""
    if not multiplier or multiplier <= 1:
        return False
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if multiplication_state(raw, multiplier) != "divisible":
        return False                      # already stamped, or genuinely at base
    new_raw = _INVENTORY_OPEN_RE.sub(
        lambda m: m.group(0) + f"<!--MLJQ-xN={multiplier}-->", raw, count=1)
    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return True


def multiply_quantities(path, multiplier, gui_state=None, out_path=None):
    """Scale every <Qty> in a file by `multiplier` and stamp it with the MLJQ-xN marker.

    Self-guarded exactly like write_plan: a file already carrying the marker, or whose every
    quantity already divides by the multiplier, is left alone — so this never doubles. Used
    to bring the leftovers Inventory file up to the same physical scale as the bags at Apply
    (the CFB master folds the inventory in as-is, so a base-scale lot would ship at 1/N).
    Returns the factor actually applied (1 when skipped)."""
    if not multiplier or multiplier <= 1:
        return 1
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if multiplication_state(raw, multiplier) != "base":
        return 1
    new_raw = _QTY_RE.sub(lambda m: f"<Qty>{int(m.group(1)) * multiplier}</Qty>", raw)
    if multiplier_stamp(new_raw) is None:
        new_raw = _INVENTORY_OPEN_RE.sub(
            lambda m: m.group(0) + f"<!--MLJQ-xN={multiplier}-->", new_raw, count=1)
    if gui_state is not None:
        new_raw = set_gui_state(new_raw, gui_state)
    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return multiplier


def _item_block(p, qty=None):
    """One <Item>…</Item> block for a catalog Part, at the file's usual indentation.
    Price is left out on purpose: BrickStore fills it from its own catalog when the file is
    opened, so it always matches the live database.

    The CATEGORY is written when the caller knows it (a lot copied from another file does —
    see restants._SrcLot), and left out otherwise, for a Part straight from the catalog, where
    BrickStore fills it in the same way. It matters because the planner sorts by category:
    a lot that reaches a bag file with no <CategoryName> sorts as the empty string, i.e. ahead
    of everything, and would be packed into the first Sac instead of beside its own kind."""
    out = ["  <Item>",
           f"   <ItemID>{escape(str(p.item_id))}</ItemID>",
           f"   <ItemTypeID>{escape(str(p.item_type))}</ItemTypeID>",
           f"   <ColorID>{int(p.color_id)}</ColorID>"]
    name = getattr(p, "item_name", "") or ""
    if name:
        out.append(f"   <ItemName>{escape(name)}</ItemName>")
    tn = _ITEM_TYPE_NAMES.get(p.item_type)
    if tn:
        out.append(f"   <ItemTypeName>{tn}</ItemTypeName>")
    if p.color_name:
        out.append(f"   <ColorName>{escape(str(p.color_name))}</ColorName>")
    cid = getattr(p, "category_id", "") or ""
    cname = getattr(p, "category_name", "") or ""
    if cid:
        out.append(f"   <CategoryID>{escape(str(cid))}</CategoryID>")
    if cname:
        out.append(f"   <CategoryName>{escape(str(cname))}</CategoryName>")
    out.append("   <Status>I</Status>")
    out.append(f"   <Qty>{int(p.qty if qty is None else qty)}</Qty>")
    out.append("   <Condition>N</Condition>")
    out.append("  </Item>")
    return "\n".join(out)


def adjust_items(path, qty_by_row=None, remove_rows=(), out_path=None):
    """Rewrite one file's lot quantities: set <Qty> for every row in `qty_by_row` and drop
    the lots listed in `remove_rows` (a row is the lot's index in document order, exactly as
    read_items numbers them). Everything else — price, dates, the multiplication marker —
    stays byte-for-byte. Returns (rows_changed, rows_removed)."""
    qty_by_row = {int(r): int(q) for r, q in (qty_by_row or {}).items()}
    remove = set(int(r) for r in (remove_rows or ()))
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    ctr = {"i": -1, "chg": 0, "del": 0}

    def repl(m):
        ctr["i"] += 1
        row = ctr["i"]
        if row in remove:
            ctr["del"] += 1
            return ""
        block = m.group(0)
        if row in qty_by_row:
            new_qty = qty_by_row[row]
            block = _QTY_RE.sub(lambda q: f"<Qty>{new_qty}</Qty>", block, count=1)
            ctr["chg"] += 1
        return block

    new_raw = _ITEM_RE.sub(repl, raw)
    if remove:
        new_raw = re.sub(r"[ \t]*\n[ \t]*\n", "\n", new_raw)
    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return ctr["chg"], ctr["del"]


def append_lots(path, lots, out_path=None):
    """Append new <Item> blocks just before </Inventory>. `lots` is a list of (part, qty)
    where part carries item_type / item_id / color_id / color_name (a catalog Part). Used to
    put a part the cataloguing missed back into the leftovers Inventory file. Returns the
    number of lots added."""
    lots = [(p, q) for p, q in lots if int(q) > 0]
    if not lots:
        return 0
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    blocks = "\n".join(_item_block(p, q) for p, q in lots)
    m = re.search(r"[ \t]*</Inventory>", raw)
    if not m:
        raise ValueError(f"{os.path.basename(path)} : pas de </Inventory> — fichier illisible")
    new_raw = raw[:m.start()] + blocks + "\n" + raw[m.start():]
    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return len(lots)


def write_inventory(path, parts, gui_state=None, currency="CAD"):
    """Write a fresh BrickStore inventory .bsx for a whole set from catalog `parts` (each
    with item_type, item_id, color_id, color_name, qty, and optionally item_name). Every
    lot is New (Condition N) and Included (Status I) at its base catalog quantity — this is
    the not-yet-catalogued starting pile. Category and Price are intentionally omitted:
    BrickStore fills them from its own catalog / price guide when the file is opened, so
    they always match the live database. The GuiState (column + sort layout) is injected
    verbatim so the file opens with the wanted columns. Returns the number of lots written."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<BrickStoreXML>",
           f' <Inventory Currency="{escape(str(currency))}">']
    n = 0
    for p in parts:
        out.append("  <Item>")
        out.append(f"   <ItemID>{escape(str(p.item_id))}</ItemID>")
        out.append(f"   <ItemTypeID>{escape(str(p.item_type))}</ItemTypeID>")
        out.append(f"   <ColorID>{int(p.color_id)}</ColorID>")
        name = getattr(p, "item_name", "") or ""
        if name:
            out.append(f"   <ItemName>{escape(name)}</ItemName>")
        tn = _ITEM_TYPE_NAMES.get(p.item_type)
        if tn:
            out.append(f"   <ItemTypeName>{tn}</ItemTypeName>")
        if p.color_name:
            out.append(f"   <ColorName>{escape(str(p.color_name))}</ColorName>")
        out.append("   <Status>I</Status>")
        out.append(f"   <Qty>{int(p.qty)}</Qty>")
        out.append("   <Condition>N</Condition>")
        out.append("  </Item>")
        n += 1
    out.append(" </Inventory>")
    block = gui_state if gui_state is not None else getattr(config, "INVENTORY_GUI_STATE", "")
    if block:
        out.append(block)
    out.append("</BrickStoreXML>")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(out) + "\n")
    return n


def remove_item_rows(raw, rows):
    """Return `raw` with the <Item> blocks at the given row indices removed (rows index
    the blocks in document order, same as read_items). Blank lines left behind are
    collapsed so the file stays clean. Used by the restants job to MOVE 'x'-marked lots
    out of a bag file (a cut, paired with a paste into the inventory)."""
    rows = set(rows)
    ctr = {"i": -1}

    def repl(m):
        ctr["i"] += 1
        return "" if ctr["i"] in rows else m.group(0)

    new_raw = _ITEM_RE.sub(repl, raw)
    return re.sub(r"[ \t]*\n[ \t]*\n", "\n", new_raw)


def write_plan(path, remarks_by_row, multiplier=1, out_path=None, force=False):
    """
    Apply a plan to a bag file in one pass: multiply every <Qty> by `multiplier` AND
    inject/replace <Remarks> for the rows in `remarks_by_row`. Everything else stays
    byte-for-byte intact.

    Multiplication is SELF-GUARDED: it only happens when multiplication_state() says
    'base' (or force=True). A file that is already 'stamped' or looks 'divisible' is
    left at its current quantities, so re-running never doubles. When we do multiply,
    the file is stamped with a MLJQ-xN marker.

    Ces deux gardes ne couvrent PAS le fichier melange : le marqueur est un commentaire XML
    que BrickStore peut retirer en reenregistrant, et il suffit alors d'UN lot rajoute a la
    quantite de base pour que les <Qty> ne divisent plus toutes par N — l'etat retombe a
    'base' et tout le fichier serait multiplie une seconde fois. On ne le devine pas ici :
    on s'arrange pour qu'un fichier melange n'existe jamais. Un set en cours de cataloguage
    est annote SANS etre multiplie (`batch.apply` passe multiplier=1 tant que
    `_catalogue_done` est faux) ; la multiplication se fait en une seule passe, a la fin,
    quand tous les sacs sont ecrits et qu'on n'y touche plus.

    Returns (lots_with_remarks, effective_multiplier, state) where effective_multiplier
    is the factor actually applied this call (1 if skipped) and state is the detected
    multiplication_state before this call.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    state = multiplication_state(raw, multiplier)
    do_mult = bool(multiplier and multiplier > 1 and (force or state == "base"))
    ctr = {"i": -1, "n": 0}

    def repl(m):
        ctr["i"] += 1
        row = ctr["i"]
        block = m.group(0)
        if do_mult:
            block = _QTY_RE.sub(
                lambda q: f"<Qty>{int(q.group(1)) * multiplier}</Qty>", block, count=1)
        if row in remarks_by_row:
            text = escape(str(remarks_by_row[row]))
            new_el = f"<Remarks>{text}</Remarks>"
            if _REMARKS_RE.search(block):
                block = _REMARKS_RE.sub(new_el, block, count=1)
            else:
                indent = re.search(r"\n([ \t]*)<Item>", "\n" + block)
                pad = indent.group(1) + " " if indent else "  "
                block = block.replace(
                    "</Item>", f"{pad}<Remarks>{text}</Remarks>\n{pad[:-1]}</Item>")
            ctr["n"] += 1
        return block

    new_raw = _ITEM_RE.sub(repl, raw)
    if do_mult and multiplier_stamp(new_raw) is None:
        new_raw = _INVENTORY_OPEN_RE.sub(
            lambda m: m.group(0) + f"<!--MLJQ-xN={multiplier}-->", new_raw, count=1)

    # Give the written bag the known-good column/sort layout so it opens correctly.
    new_raw = set_gui_state(new_raw)

    with open(out_path or path, "w", encoding="utf-8", newline="") as f:
        f.write(new_raw)
    return ctr["n"], (multiplier if do_mult else 1), state
