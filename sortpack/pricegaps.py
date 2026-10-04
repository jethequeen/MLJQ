# -*- coding: utf-8 -*-
"""
Find catalog items whose price is MISSING from BrickStore's local price-guide cache
(priceguide_cache.sqlite) and emit them as a single BrickStore .bsx document, so the
whole gap can be filled in ONE bulk "Update Price Guide" instead of parting out sets
one by one.

The gap matters most for set-EXCLUSIVE minifigures: BrickStore only caches prices for
items it has downloaded, so recent/exclusive figures (which carry much of a big set's
value) price as $0 and quietly understate the set. See setvalue.set_value — an item
with no price contributes nothing.

Workflow:
  1. `python value.py pricegaps`  -> writes price_gaps.bsx (missing minifigs in the feed)
  2. open it in BrickStore, select all (Ctrl+A), Update Price Guide, wait, close
  3. re-run the value/buy report — the figures now count, big sets re-value correctly
"""

import os

from . import config


def missing_items(cat, pg, set_nos, types=("M",)):
    """Deduped list of Part whose price_key() is absent from the price guide, drawn from
    the inventories of `set_nos`. `types` limits item types ('M' = minifigs; pass None
    for every type). Only the catalog's real sets are scanned; unknowns are skipped."""
    seen = set()
    out = []
    for s in set_nos:
        s = str(s).strip()
        if not cat.has_set(s):
            continue
        for p in cat.inventory(s):
            if types is not None and p.item_type not in types:
                continue
            k = (p.item_type, p.item_id, p.color_id)
            if k in seen:
                continue
            seen.add(k)
            if pg.get(p.price_key()) is None:
                out.append(p)
    return out


def write_bsx(items, path, currency="CAD"):
    """Write `items` (Part list) as a minimal BrickStore document — just the identifiers
    BrickStore needs to resolve each item and fetch its price guide. Returns the count."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<BrickStoreXML>",
             f' <Inventory Currency="{currency}">']
    for p in items:
        lines += [
            "  <Item>",
            f"   <ItemID>{p.item_id}</ItemID>",
            f"   <ItemTypeID>{p.item_type}</ItemTypeID>",
            f"   <ColorID>{p.color_id}</ColorID>",
            "   <Status>I</Status>",
            "   <Qty>1</Qty>",
            "   <Price>0.000</Price>",
            "   <Condition>N</Condition>",
            "  </Item>",
        ]
    lines += [" </Inventory>", "</BrickStoreXML>", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(items)


def build(cat, pg, set_nos, out_path=None, include_parts=False):
    """Scan `set_nos` for price-cache gaps and write them to a .bsx. Returns
    (path, n_items). Default output: price_gaps.bsx in the working directory."""
    types = None if include_parts else ("M",)
    items = missing_items(cat, pg, set_nos, types=types)
    path = out_path or os.path.join(os.getcwd(), "price_gaps.bsx")
    write_bsx(items, path)
    return path, len(items)
