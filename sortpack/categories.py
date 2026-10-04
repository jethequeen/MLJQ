# -*- coding: utf-8 -*-
"""
Where a part's CATEGORY comes from when the catalog can't tell us.

Phase A sorts the whole inventory by Type -> Category -> Description -> Color, so a lot
with no category sorts as the empty string — ahead of everything — and would be packed
into the first Sac instead of beside its own kind.

BrickStore writes ``<CategoryName>`` into a ``.bsx`` when it opens the file, and it is the
authority. But two things reach us without one:

* a lot we append ourselves, before BrickStore has ever seen that file;
* a part taken from the catalog blob, because ``catalogdb`` does not decode the category
  table at all (it reads the item id, type, name and years only).

A category is a property of the MOULD, not of a lot, so any ``.bsx`` anywhere that mentions
the same ItemID answers the question. This module harvests that map from the files we
already have — every set's bags and inventory, plus the stray exports in the project folder
(``price_gaps.bsx`` alone carries thousands of lots). It is a fallback, not a source of
truth: ``build_plan`` prefers a category read from the very file it is planning.
"""

import os
import re
import glob
import html

from . import config

# <ItemID>..</ItemID> ... <CategoryID>..</CategoryID><CategoryName>..</CategoryName> inside
# one <Item> block. Scanned with a regex rather than an XML parse: this runs over every .bsx
# we own and only the three tags matter.
_ITEM_RE = re.compile(r"<Item>(.*?)</Item>", re.DOTALL)
_ID_RE = re.compile(r"<ItemID>([^<]*)</ItemID>")
_CID_RE = re.compile(r"<CategoryID>([^<]*)</CategoryID>")
_CNAME_RE = re.compile(r"<CategoryName>([^<]*)</CategoryName>")

_CACHE = None


def _bsx_files():
    """Every .bsx we might learn a category from: the catalogued sets and any export sitting
    in the project folder.

    Les sets passent par `plan.iter_set_folders`, le seul endroit qui sait ou ils vivent :
    depuis les batchs, un set est deux niveaux plus bas (CFB/Batchs/<nom>/<set>/) et un glob
    en `*/*.bsx` ne le voyait plus. Consequence silencieuse : ses propres fichiers, qui
    portent la categorie de chacun de ses moules, disparaissaient de la moisson -- et la
    Phase A calculee depuis le catalogue (forecast, restants) triait ces lots comme la
    chaine vide, donc au mauvais endroit."""
    seen, out = set(), []

    def take(pattern):
        for path in glob.glob(pattern):
            if path not in seen:
                seen.add(path)
                out.append(path)

    roots = [config.CATALOGUAGE_ROOT, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        take(os.path.join(root, "*.bsx"))
        take(os.path.join(root, "*", "*.bsx"))
    try:
        from .plan import iter_set_folders, batch_root
        for folder in iter_set_folders():
            take(os.path.join(folder, "*.bsx"))
        take(os.path.join(batch_root(), "*", "*.bsx"))   # les maitres / consolidations
    except Exception:
        pass                       # la moisson est un secours : elle ne casse jamais un run
    return out


def load(refresh=False):
    """{item_id: (category_id, category_name)} harvested from every .bsx we own.

    Built once per process (it is only needed for the rare part nothing on disk describes)
    and deliberately forgiving: an unreadable file is skipped, never fatal."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE
    out = {}
    for path in _bsx_files():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        for block in _ITEM_RE.findall(raw):
            m = _ID_RE.search(block)
            if not m:
                continue
            item_id = m.group(1).strip()
            if not item_id or item_id in out:
                continue
            cn = _CNAME_RE.search(block)
            if not cn or not cn.group(1).strip():
                continue
            ci = _CID_RE.search(block)
            # Les tags sont lus a la regex, donc bruts : « Food &amp; Drink » ici et
            # « Food & Drink » chez bsx.read_items, qui passe par un parseur XML. Deux
            # chaines differentes pour une meme categorie, donc deux places au tri.
            out[item_id] = ((ci.group(1).strip() if ci else ""),
                            html.unescape(cn.group(1).strip()))
    _CACHE = out
    return _CACHE


def category_of(item_id, refresh=False, catalog=True):
    """(category_id, category_name) pour un ItemID, ("", "") si personne ne sait.

    Le CATALOGUE d'abord : c'est la source vivante, et c'est elle que BrickStore ecrira dans
    le .bsx la prochaine fois qu'il l'ouvrira. Nos fichiers, eux, gardent ce qu'il y ecrivait
    le jour ou ils ont ete faits — 32803 y est encore « Slope, Curved » quand le catalogue
    dit « Slope, Curved, Inverted ». Planifier sur la vieille valeur placerait le lot ailleurs
    que la ou il finira.

    La moisson reste le secours pour ce que le blob ne categorise pas, et `catalog=False`
    la redonne seule (utile pour comparer les deux sources)."""
    if catalog:
        try:
            from . import catalogdb
            cat = catalogdb.shared()
            idx = cat.item_index(str(item_id))
            if idx is not None:
                cid, cname = cat.category_of_index(idx)
                if cname:
                    return (cid, cname)
        except Exception:
            pass                   # catalogue absent/illisible : la moisson fait le travail
    return load(refresh=refresh).get(str(item_id), ("", ""))


def main():
    """`python -m sortpack.categories [item_id ...]` — what the harvest knows."""
    import sys
    m = load()
    print(f"{len(m)} ItemID(s) avec une categorie, depuis {len(_bsx_files())} fichier(s) .bsx")
    for item_id in sys.argv[1:]:
        cid, cname = category_of(item_id)
        print(f"  {item_id:14} -> {cname or '(inconnue)'}{f' [{cid}]' if cid else ''}")


if __name__ == "__main__":
    main()
