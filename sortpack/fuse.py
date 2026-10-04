# -*- coding: utf-8 -*-
"""
FUSION RÉTROACTIVE — réunir des sets déjà triés séparément en un seul envoi.

Le flux normal déclare un batch AVANT le tri, et il n'y a alors rien à fusionner (voir
sortpack/batch.py). Ce module est le rattrapage : des sets ont été triés chacun de leur côté,
et il faut maintenant les réunir sans tout re-trier.

La méthode : on calcule d'abord la Phase A sur l'inventaire COMBINÉ — même tri
Type → Catégorie → Description → Couleur, mêmes 650 g, un moule reste atomique — puis on
émet **un fichier d'instructions par set, dans l'ordre où on va les traiter**. Le premier dit
comment diviser le set de base en Sacs ; les suivants disent dans quel Sac verser chaque lot.
Les Sacs sont donc dimensionnés dès le départ pour le total, et non pour le premier set.

**Consolidation par MOULE seulement.** Pas de boîte C : les sets sont déjà triés, donc un lot
(pièce + couleur) est déjà entier — il n'y a rien à ramasser à travers les sacs. Ce qui reste
est le regroupement que le client demande : toutes les couleurs d'un moule dans un sous-sac.
L'étiquette le nomme et liste ses couleurs, identiques d'un fichier à l'autre, donc le
sous-sac commencé par le premier set est complété par les suivants.

La source est le **maître** de chaque set (`<numéro>.bsx`) : il est à l'échelle physique, il
contient exactement ce qui a été expédié, et il survit à la mise à la corbeille du dossier.

    python -m sortpack.fuse 43011 42680 77256
"""

import os
import re

from . import config
from . import bsx
from .plan import item_sort_tuple, mould_key, _sac, _pad

# L'étiquette porte D'ABORD l'emplacement ACTUEL du lot, puis sa destination. Dans cet
# ordre parce que c'est l'ordre du travail : on ouvre un Sac du tri d'origine et on le vide
# dans les nouveaux, qui sont tous étalés. Le tri texte sur Remarks regroupe donc par Sac
# d'origine — un Sac à la fois, rien à chercher.
FUSE_FROM = "{set}-{src} → Sac {sac}"
FUSE_GROUP = "{set}-{src} → Sac {sac} · {mould} ({colors})"
# Un lot qui n'a jamais eu de Sac écrit : c'était un restant, placé à l'œil.
FUSE_LEFTOVER = "restants"
# Le MAÎTRE, lui, part chez le client : il ne dit que la DESTINATION. D'où le lot sortait
# chez nous ne le regarde pas et ne voudrait rien dire pour lui — c'est notre parcours de
# travail, pas le contenu de l'envoi.
MASTER_SAC = "Sac {sac}"
MASTER_GROUP = "Sac {sac} · {mould} ({colors})"
# Un moule peut avoir treize couleurs (le 3023 en a treize dans ces trois sets) : les lister
# toutes rend la remarque illisible. On en nomme trois et on annonce le reste — ce qu'il faut
# pour reconnaître le sous-sac et savoir combien il en manque encore.
FUSE_MAX_COLORS = 3


def _master(num, out_dir=None):
    return os.path.join(out_dir or config.CFB_OUTPUT_DIR, f"{num}.bsx")


def read_master(num, out_dir=None):
    """{clé: item} + le bloc <Item> d'origine, depuis le maître déjà construit."""
    path = _master(num, out_dir)
    if not os.path.exists(path):
        raise RuntimeError(f"maître introuvable : {path}")
    blocks, raw = bsx.read_item_blocks(path)
    cur = re.search(r'<Inventory[^>]*\bCurrency="([^"]+)"', raw)
    lots = {}
    for it in bsx.read_items(path):
        k = (it["item_id"], it["color_id"], it["condition"])
        if k in lots:                      # deux lignes du même lot : on cumule
            lots[k]["qty"] += it["qty"]
            continue
        lots[k] = {"qty": it["qty"], "item": it, "block": blocks[it["row"]]}
    return lots, (cur.group(1) if cur else "CAD")


def origin_sacs(num):
    """Où se trouve AUJOURD'HUI chaque lot de ce set, d'après son tri d'origine.

    Le maître ne le dit pas — ses remarques sont supprimées à la construction — et le dossier
    du set est à la corbeille. On le relit donc dans la sauvegarde la plus complète, par trois
    chemins successifs :

      1. la remarque terminale du lot lui-même (« … -> Sac 05 ») ;
      2. sinon, le Sac de son MOULE : une couleur restée en boîte D (« D01 ») part avec ses
         sœurs, et Phase A garantit qu'un moule finit dans un seul Sac ;
      3. sinon, la remarque de l'inventaire des restants, quand la sauvegarde en garde une.

    Ce qui reste introuvable était un restant placé à l'œil : on le dit, plutôt que d'inventer
    un numéro. Renvoie {clé: numéro de Sac ou None}."""
    from .plan import discover_bags, find_inventory, existing_sacs
    bk = os.path.join(config.CATALOGUAGE_ROOT, "backups")
    if not os.path.isdir(bk):
        return {}
    best, best_n, invs = None, 0, []
    for stamp in sorted(os.listdir(bk)):
        d0 = os.path.join(bk, stamp)
        if not os.path.isdir(d0):
            continue
        for n in os.listdir(d0):
            d = os.path.join(d0, n)
            if not (n.startswith(str(num)) and os.path.isdir(d)):
                continue
            nb = len([x for x in os.listdir(d)
                      if x.lower().endswith(".bsx") and x[:1].isdigit()])
            if nb >= best_n:                       # le plus complet, et le plus récent à égalité
                best, best_n = d, nb
            inv = find_inventory(d)
            if inv:
                invs.append(inv)
    if not best or not best_n:
        return {}

    loc = existing_sacs(discover_bags(best))
    by_mould = {}
    for k, s in loc.items():
        by_mould.setdefault(mould_key(k), s)
    for inv in invs:                               # la plus récente qui porte des remarques
        for it in bsx.read_items(inv):
            m = re.findall(r"Sac (\d+)", it["remarks"] or "")
            if m:
                loc[(it["item_id"], it["color_id"], it["condition"])] = int(m[-1])
    return loc, by_mould


def plan_merge(nums, wdb, out_dir=None):
    """Phase A sur l'union des sets. Renvoie (sac_of, meta, per_set, colors, currency).

    `sac_of` est indexé par lot de l'inventaire combiné : un lot présent dans deux sets n'y
    figure qu'une fois, puisqu'il finira dans un seul Sac."""
    per_set, currency = {}, "CAD"
    for n in nums:
        per_set[n], cur = read_master(n, out_dir)
        currency = cur or currency

    comb, meta = {}, {}
    for n in nums:
        for k, v in per_set[n].items():
            comb[k] = comb.get(k, 0) + v["qty"]
            meta.setdefault(k, v["item"])

    weight = {k: (wdb.weight(k[0], meta[k]["item_name"]) or 0.0) for k in comb}
    grams = {k: weight[k] * comb[k] for k in comb}

    # un moule reste atomique, sauf s'il pèse à lui seul plus qu'un Sac (on le scinde alors,
    # comme le fait build_plan : mieux vaut deux sous-sacs qu'un Sac hors gabarit)
    groups = {}
    for k in sorted(comb, key=lambda kk: item_sort_tuple(meta[kk])):
        groups.setdefault(mould_key(k), []).append(k)

    sac_of, sac, run = {}, 1, 0.0
    for keys in groups.values():
        gw = sum(grams[k] for k in keys)
        if len(keys) >= 2 and gw <= config.SAC_TARGET_GRAMS:
            if run > 0 and run + gw > config.SAC_TARGET_GRAMS:
                sac += 1
                run = 0.0
            for k in keys:
                sac_of[k] = sac
            run += gw
            continue
        for k in keys:
            if run > 0 and run + grams[k] > config.SAC_TARGET_GRAMS:
                sac += 1
                run = 0.0
            sac_of[k] = sac
            run += grams[k]

    # les couleurs de chaque moule DANS L'ENSEMBLE : c'est le contenu final du sous-sac, donc
    # le premier fichier annonce déjà les couleurs qu'apporteront les suivants
    colors = {}
    for m, keys in groups.items():
        if len(keys) > 1:
            colors[m] = sorted(meta[k]["color_name"] or "?" for k in keys)
    return sac_of, meta, per_set, colors, currency, grams


def remark_for(k, num, sac_of, colors, loc, by_mould):
    """« 43011-03 → Sac 07 » : d'où on le sort, où on le met. Avec « · moule (couleurs) »
    quand il faut en faire un sous-sac."""
    src = loc.get(k)
    if src is None:
        src = by_mould.get(mould_key(k))
    src = f"{int(src):0{config.LABEL_PAD}d}" if src is not None else FUSE_LEFTOVER
    n = f"{int(sac_of[k]):0{config.LABEL_PAD}d}"
    m = mould_key(k)
    if m in colors:
        return FUSE_GROUP.format(set=num, src=src, sac=n, mould=m[0],
                                 colors=_colors(colors[m]))
    return FUSE_FROM.format(set=num, src=src, sac=n)


def _colors(names):
    """« Black, Blue, Green +10 » : trois couleurs et le compte du reste. La troncature est
    la même pour un moule donné dans tous les fichiers, donc l'étiquette reste identique et
    le tri les rassemble."""
    head = list(names)[:FUSE_MAX_COLORS]
    rest = len(names) - len(head)
    return ", ".join(head) + (f" +{rest}" if rest > 0 else "")


def write_files(nums, wdb, out_dir=None, dest=None, log=print):
    """Un fichier d'instructions par set, dans l'ordre donné.

    Le premier set est celui qu'on DIVISE (il n'existe encore aucun Sac) ; les suivants
    s'intègrent dans les Sacs déjà en place."""
    from .restants import _emit_item, _document
    sac_of, meta, per_set, colors, currency, grams = plan_merge(nums, wdb, out_dir)
    dest = dest or os.path.join(config.BATCH_ROOT, "Fusion " + "+".join(nums))
    os.makedirs(dest, exist_ok=True)

    nsac = max(sac_of.values())
    total_g = sum(grams.values())
    log(f"Inventaire combiné : {len(sac_of)} lots, {total_g/1000:.2f} kg → {nsac} Sacs "
        f"(cible {config.SAC_TARGET_GRAMS:.0f} g)")
    log(f"Sous-sacs à faire : {len(colors)} moules multi-couleurs")

    out = []
    unknown_total = 0
    for i, n in enumerate(nums, 1):
        verb = "diviser" if i == 1 else "integrer"
        loc, by_mould = origin_sacs(n) or ({}, {})
        unknown = [k for k in per_set[n]
                   if loc.get(k) is None and by_mould.get(mould_key(k)) is None]
        unknown_total += len(unknown)
        texts = []
        # trié par emplacement d'ORIGINE : on vide un Sac du tri d'origine à la fois
        def _key(kk):
            s = loc.get(kk)
            if s is None:
                s = by_mould.get(mould_key(kk))
            return (999 if s is None else s, sac_of[kk], item_sort_tuple(meta[kk]))
        for k in sorted(per_set[n], key=_key):
            texts.append(_emit_item(per_set[n][k]["block"], per_set[n][k]["qty"],
                                    remark_for(k, n, sac_of, colors, loc, by_mould)))
        path = os.path.join(dest, f"Fusion {i} - {verb} {n}.bsx")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(bsx.set_gui_state(_document(currency, texts)))
        sacs = sorted({sac_of[k] for k in per_set[n]})
        subs = len({mould_key(k) for k in per_set[n] if mould_key(k) in colors})
        srcs = sorted({loc.get(k) or by_mould.get(mould_key(k)) for k in per_set[n]}
                      - {None})
        log(f"  {i}. {n} : {len(texts)} lots, depuis ses Sacs "
            f"{srcs[0]:02d}–{srcs[-1]:02d} → Sacs {sacs[0]:02d}–{sacs[-1]:02d} "
            f"({subs} sous-sacs)"
            + (f"  ⚠ {len(unknown)} restant(s) sans emplacement" if unknown else "")
            + f" → {os.path.basename(path)}")
        out.append(path)
    log(f"Dossier : {dest}")
    if unknown_total:
        log(f"⚠ {unknown_total} lot(s) marqués « {FUSE_LEFTOVER} » : c'étaient des restants, "
            f"placés à l'œil, aucun Sac n'a jamais été écrit pour eux.")
    return {"files": out, "dir": dest, "sacs": nsac, "lots": len(sac_of),
            "grams": total_g, "groups": len(colors), "unknown": unknown_total}


def master_remark(k, sac_of, colors):
    """L'étiquette côté client : son Sac, et le sous-sac quand le moule en demande un."""
    n = f"{int(sac_of[k]):0{config.LABEL_PAD}d}"
    m = mould_key(k)
    if m in colors:
        return MASTER_GROUP.format(sac=n, mould=m[0], colors=_colors(colors[m]))
    return MASTER_SAC.format(sac=n)


def write_master(nums, wdb, out_dir=None, dest=None, name=None, log=print):
    """LE fichier qui part chez le client : tout l'envoi fusionné, en UN inventaire.

    C'est le pendant de `finalize.build_cfb_files` pour une fusion. Celui-là part des
    dossiers de sets et de leurs sacs numérotés ; ici les sets sont déjà expédiés, leurs
    dossiers sont à la corbeille, et la source est le MAÎTRE de chacun — à l'échelle
    physique, exactement ce qui a été envoyé.

    Ce que le maître fait, et que les fichiers d'instructions ne font pas :

    * **un lot partagé par deux sets devient UNE ligne.** C'est tout l'intérêt de l'envoi
      groupé : le client range sa pièce une fois, pas une fois par set ;
    * **l'étiquette ne porte que la destination** (`Sac 04 · 3023 (Black, Blue, … +10)`).
      Le `43011-02 →` des fichiers d'instructions est notre parcours à nous ;
    * **les lignes sortent dans l'ordre des Sacs**, puis dans l'ordre du catalogue à
      l'intérieur de chacun — l'ordre dans lequel il vide l'envoi.
    """
    from .restants import _emit_item, _document
    sac_of, meta, per_set, colors, currency, grams = plan_merge(nums, wdb, out_dir)

    # fusion des quantités ET choix du bloc source : le premier set qui porte le lot donne
    # son <Item> (prix, condition, catégorie), les suivants n'ajoutent que des pièces.
    qty, block = {}, {}
    for n in nums:
        for k, v in per_set[n].items():
            qty[k] = qty.get(k, 0) + v["qty"]
            block.setdefault(k, v["block"])

    order = sorted(qty, key=lambda kk: (sac_of[kk], item_sort_tuple(meta[kk])))
    texts = [_emit_item(block[k], qty[k], master_remark(k, sac_of, colors)) for k in order]

    name = name or ("Fusion " + "+".join(nums))
    dest = dest or os.path.join(config.BATCH_ROOT, "Fusion " + "+".join(nums))
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"{name}.bsx")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(bsx.set_gui_state(_document(currency, texts)))

    nsac = max(sac_of.values())
    shared = sum(1 for k in qty if sum(1 for n in nums if k in per_set[n]) > 1)
    total_q = sum(qty.values())
    log(f"Maître « {name} » : {len(texts)} lots, {total_q} pièces, "
        f"{sum(grams.values())/1000:.2f} kg → Sacs 01–{nsac:02d}, {len(colors)} sous-sacs")
    for n in nums:
        log(f"  {n} : {len(per_set[n])} lots, {sum(v['qty'] for v in per_set[n].values())} pièces")
    if shared:
        log(f"  {shared} lot(s) présents dans plusieurs sets, fusionnés en une seule ligne")
    log(f"  → {path}")
    return {"path": path, "lots": len(texts), "qty": total_q, "sacs": nsac,
            "groups": len(colors), "shared": shared, "grams": sum(grams.values()),
            "per_set": {n: {"lots": len(per_set[n]),
                            "qty": sum(v["qty"] for v in per_set[n].values())} for n in nums}}


def main():
    import argparse
    from .weightdb import WeightDB
    ap = argparse.ArgumentParser(
        description="Fusionner des sets deja tries en un seul envoi (fichiers d'instructions).")
    ap.add_argument("sets", nargs="+", help="numeros, dans l'ordre de traitement")
    ap.add_argument("--dest", help="dossier de sortie")
    ap.add_argument("--master", action="store_true",
                    help="ecrire LE maitre a envoyer au client au lieu des instructions")
    ap.add_argument("--name", help="nom du fichier maitre (defaut: Fusion a+b+c)")
    args = ap.parse_args()
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if args.master:
        write_master(args.sets, WeightDB(), dest=args.dest, name=args.name)
    else:
        write_files(args.sets, WeightDB(), dest=args.dest)


if __name__ == "__main__":
    main()
