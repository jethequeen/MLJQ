# -*- coding: utf-8 -*-
"""
Build the final file to send to Canada First Brick (CFB), run AFTER sorting/packing.

Takes a set folder full of numbered bag files (whose <Remarks> now hold the packing
plan, e.g. "Sac 09") PLUS the processed leftovers inventory (Inventory*.bsx), and writes
one consolidated master list in CFB_OUTPUT_DIR:

  <set-number>.bsx              the MASTER list to send to CFB

Consolidation key: (ItemID, ColorID, Condition). Quantities are summed across every
numbered bag and the inventory file.

Cleanup rule ("x"): a lot whose <Remarks> is exactly "x" is a piece we no longer have.
Normally "Calculer les restants" (sortpack/restants.py) has already MOVED those out of
the bags into the inventory to be re-checked; any "x" still present here (in a bag or in
the inventory) is a piece confirmed gone — it is dropped from the master and NOT written
to any restants file (the leftovers are handled entirely by the inventory flow now).

Each output <Item> preserves its original block (Price, LotID, DateAdded, …); we only
rewrite <Qty> to the summed value and drop <Remarks>.

email_files() POSTs the files (base64) to a Google Apps Script Web App that mails them
back as attachments. Nothing is sent unless CFB_APPS_SCRIPT_URL is configured.
"""

import os
import re
import json
import base64
import urllib.request

from . import config
from . import bsx
from .plan import discover_bags, set_number, find_inventory


_QTY_RE = re.compile(r"<Qty>\s*\d+\s*</Qty>")
_REMARKS_RE = re.compile(r"<Remarks>.*?</Remarks>", re.DOTALL)
_CURRENCY_RE = re.compile(r'<Inventory[^>]*\bCurrency="([^"]+)"')


def planned_paths(set_folder, out_dir=None):
    """The master + restants paths this set WOULD write to (candidates, may not exist).
    Lets the GUI warn before overwriting."""
    out_dir = out_dir or config.CFB_OUTPUT_DIR
    num = set_number(set_folder)
    return (os.path.join(out_dir, f"{num}.bsx"),
            os.path.join(out_dir, f"{num} - restants.bsx"))


def _currency(bags):
    """Reuse the Currency of the first bag file; default CAD."""
    for _, path, _ in bags:
        _, raw = bsx.read_item_blocks(path)
        m = _CURRENCY_RE.search(raw)
        if m:
            return m.group(1)
    return "CAD"


def _emit_item(raw_block, qty):
    """Re-emit one <Item> block with a new Qty, no Remarks, canonical indentation."""
    block = _REMARKS_RE.sub("", raw_block)
    block = _QTY_RE.sub(f"<Qty>{qty}</Qty>", block, count=1)
    out = []
    for ln in (l.strip() for l in block.splitlines()):
        if not ln:
            continue
        pad = "  " if ln.startswith("<Item>") or ln.startswith("</Item>") else "   "
        out.append(pad + ln)
    return "\n".join(out)


def _document(currency, item_texts):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<BrickStoreXML>",
             f' <Inventory Currency="{currency}">']
    lines.extend(item_texts)
    lines += [" </Inventory>", "</BrickStoreXML>", ""]
    return "\n".join(lines)


def _validate_before_build(set_folder, log):
    """Make the files sound before they become the master. Returns (report, warnings).

    Two things, in this order, and both matter here specifically:

    1. **Scale.** The leftovers Inventory is written at BASE (one copy) while the bags are
       multiplied ×N at Apply — and this function folds the inventory in AS-IS, so an
       un-normalised leftover ships at 1/N. Apply normally normalises it, but a set whose
       sorting has already started is skipped by auto-apply and may never go through an
       Apply again, so the master would quietly go out short.
    2. **Counts.** The same cross-check Apply runs (verify.before_apply): lots missing from
       the files are put back, surplus taken off. On an already-multiplied set the checker
       is read-only by design, so it can only REPORT — that report is handed back as a
       warning instead of being swallowed, because a gap here means a wrong master.
    """
    from . import verify
    warnings = []
    try:
        mult = verify.set_multiplier(set_number(set_folder)) or 1
    except Exception as e:
        warnings.append(f"⚠ ×Quantité inconnue ({e}) — échelle non vérifiée")
        return None, warnings

    try:
        # force : on construit le maitre, l'inventaire part avec lui et doit donc etre a
        # l'echelle physique, meme si le cataloguage n'a jamais ete declare fini.
        n = verify.normalize_inventory(set_folder, mult, log=log, force=True)
        if n > 1:
            warnings.append(f"• Inventaire mis à l'échelle physique ×{n} avant l'envoi "
                            f"(il serait parti à 1/{n}).")
    except Exception as e:
        warnings.append(f"⚠ Mise à l'échelle de l'inventaire impossible : {e}")

    report = None
    try:
        report = verify.before_apply(set_folder, log=log, set_no=set_number(set_folder))
    except Exception as e:
        warnings.append(f"⚠ Vérification impossible : {e}")
        return None, warnings

    if report and report.get("diffs") and not report.get("fixed"):
        warnings.append(
            f"⚠ {len(report['diffs'])} lot(s) en écart ({report['gap_qty']} pièce(s)) "
            f"NON corrigés — le maître part avec cet écart"
            + (f" : {report['blocked']}" if report.get("blocked") else ""))
    return report, warnings


def build_cfb_files(set_folder, out_dir=None, log=None, check=None, name=None):
    """
    Consolidate the set's bags, split on the "x" cleanup mark, and write the master +
    restants .bsx into out_dir (default CFB_OUTPUT_DIR). Returns a result dict with
    the paths (restants_path is None when there are no leftovers) and summary stats.

    `set_folder` is ONE set folder or a LIST of them — a BATCH. Several sets shipped
    together consolidate into a SINGLE master, which is the whole point: the customer files
    his inventory in one pass instead of one per set. Lots shared between sets merge into one
    line (116 of them across a four-set batch), so he never handles the same part twice.
    `name` is what the file is called; it defaults to the set number, or, for a batch, must
    be given.

    The files are validated FIRST, because this is the last moment before the master
    leaves: the leftovers are brought to the bags' ×N scale and the set is reconciled
    against BrickStore's catalog. `check=False` skips it (config.CFB_VERIFY_ON_BUILD is
    the default). `result["verify"]` carries what was found, and `result["warnings"]`
    the lines worth showing beside the "envoyé" message.
    """
    out_dir = out_dir or config.CFB_OUTPUT_DIR
    log = log or (lambda _m: None)
    folders = [set_folder] if isinstance(set_folder, str) else list(set_folder)
    warnings = []
    vreport = None
    if check is None:
        check = getattr(config, "CFB_VERIFY_ON_BUILD", True)
    if check:
        for f in folders:
            r, w = _validate_before_build(f, log)
            vreport = vreport or r
            warnings.extend(w)

    bags = []
    for f in folders:
        bags.extend(discover_bags(f))
    mark = config.CFB_MISSING_REMARK.strip().casefold()

    # The master is the numbered bags PLUS the processed leftovers inventory (its 'x' lots
    # were already moved here by "Calculer les restants"; anything still marked 'x'
    # anywhere is a piece we no longer have — it is dropped, not sold, and there is no
    # separate restants file).
    invs = [p for p in (find_inventory(f) for f in folders) if p]
    inv_path = invs[0] if invs else None
    files = list(bags)
    for p in invs:
        files.append(("Inventory", p, bsx.read_items(p)))

    present = {}                       # key -> [qty, representative_block]
    order_present = []
    dropped_lots = dropped_qty = 0
    for _, path, items in files:
        blocks, _ = bsx.read_item_blocks(path)
        for it in items:
            if it["remarks"].strip().casefold() == mark:
                dropped_lots += 1
                dropped_qty += it["qty"]
                continue
            key = (it["item_id"], it["color_id"], it["condition"])
            if key not in present:
                present[key] = [0, blocks[it["row"]]]
                order_present.append(key)
            present[key][0] += it["qty"]

    currency = _currency(files)
    # Un batch n'a pas de numéro de set : il porte son nom. Un set seul garde le sien, donc
    # les fichiers déjà produits ne changent pas de nom.
    num = name or set_number(folders[0])
    os.makedirs(out_dir, exist_ok=True)

    master_items = [_emit_item(present[k][1], present[k][0]) for k in order_present]
    master_path = os.path.join(out_dir, f"{num}.bsx")
    with open(master_path, "w", encoding="utf-8", newline="") as f:
        f.write(_document(currency, master_items))

    return {
        "set_number": num,
        "master_path": master_path,
        "restants_path": None,
        "master_lots": len(order_present),
        "master_qty": sum(present[k][0] for k in order_present),
        "restants_lots": dropped_lots,
        "restants_qty": dropped_qty,
        "included_inventory": bool(inv_path),
        "bags": len(bags),
        "verify": vreport,
        "warnings": warnings,
        # An uncorrected gap means the master went out wrong. Nothing may throw the set
        # folder away while that is true — it is the only remaining evidence.
        "verify_gap": bool(vreport and vreport.get("diffs") and not vreport.get("fixed")),
    }


def email_files(paths, to=None, subject=None, message=None):
    """
    POST the given files (base64) to the Apps Script Web App, which emails them as
    attachments. Raises if CFB_APPS_SCRIPT_URL is not configured. Returns the script's
    text response ('ok' on success).
    """
    url = config.CFB_APPS_SCRIPT_URL
    if not url:
        raise RuntimeError("CFB_APPS_SCRIPT_URL non configuré dans sortpack/config.py")

    files = []
    for p in paths:
        with open(p, "rb") as f:
            files.append({"filename": os.path.basename(p),
                          "contentB64": base64.b64encode(f.read()).decode("ascii")})

    payload = {
        "token": config.CFB_APPS_SCRIPT_TOKEN,
        "to": to or config.CFB_EMAIL_TO,
        "subject": subject or "Fichier CFB",
        "message": message or "",
        "files": files,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace").strip()
