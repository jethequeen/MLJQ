# -*- coding: utf-8 -*-
"""
Le PLAN PREVISIONNEL d'un batch — la Phase A calculée depuis le CATALOGUE, avant que les
sets soient arrivés et catalogués.

Pourquoi
--------
La numérotation des Sacs d'un batch court sur l'UNION de ses sets (voir sortpack/batch.py),
donc tant qu'il manque un set on ne peut pas trier : un plan fait sur une partie de l'envoi
ne vaut que pour cette partie, et le repiner plus tard entasserait le reste de l'envoi dans
des Sacs dimensionnés pour une fraction (pinned_sac_of n'ouvre JAMAIS de nouveau Sac).

Or l'information existe d'avance : l'inventaire d'un set sort du catalogue BrickStore
(`catalogdb.inventory`, la même référence que verify) et les grammes de `weightdb`. Le seul
élément qui manque au catalogue est le nombre d'exemplaires — il vient de la table
`inventory` de l'historique, alimentée par les achats Catégorie A du Journal. D'où la règle :

    on peut figer le plan dès que les ACHATS SONT PASSÉS, pas sur les candidats du classement.

Ce que ça donne
---------------
`compute()` rend {clé de lot -> numéro de Sac} sur l'union des sets prévus, plus de quoi
juger le plan (Sacs, grammes, lots sans poids, lots sans catégorie). `save()` le range dans
le dossier du batch ; `batch.plan()` le passe ensuite à `build_plan(pinned_layout=...)`, si
bien qu'un set trié aujourd'hui porte déjà les numéros de Sacs de l'envoi complet.

Ce qu'il ne calcule PAS, et pourquoi
------------------------------------
La **Phase B** — les boîtes C, « garder cette couleur, elle revient » — est hors de portée.
Elle parcourt les **sacs numérotés** du set dans l'ordre et décide, lot par lot, si la couleur
revient plus loin. Or le catalogue donne l'inventaire TOTAL d'un set, jamais sa répartition
entre les sacs de la boîte : cette répartition est précisément ce que le cataloguage découvre.
Sans elle, aucune des questions de la Phase B n'a de réponse.

La **passe de consolidation finale**, elle, est calculable d'avance : elle ne demande que
« quelles couleurs d'un même moule finissent dans le même Sac », et c'est Phase A qui le
décide. Le rapport l'annonce donc — nombre de sous-sacs, et le Sac qui en demande le plus.

Les limites, à assumer
----------------------
* le plan ne vaut que pour la composition prévue : un set substitué, une rupture ou une
  quantité qui change décalent la numérotation, et ce qui est déjà trié devient faux. D'où
  `verify_composition()`, qui compare le plan à ce que le registre dit aujourd'hui ;
* la catégorie ne se décode pas depuis le blob du catalogue : elle vient de
  `sortpack.categories`, qui la moissonne dans les .bsx qu'on possède. Un moule jamais vu
  n'a pas de catégorie et se trie comme la chaîne vide — `compute()` compte ces lots et le
  rapport les affiche, parce qu'ils sont la seule source d'écart avec le plan final ;
* les pièces en trop (`include_extras=True`) sont comptées : la boîte les contient
  physiquement et on les catalogue. C'est le choix de verify.py, tenu ici aussi.
"""

import os
import io
import json
import datetime

from . import config
from .plan import flow_sacs, mould_key

FORECAST_FILE = "plan_previsionnel.json"
CONDITION = "N"          # un set scellé : tout est neuf (cf. les .bsx catalogués)


def _key(item_id, color_id):
    """La même clé que plan.part_key — et donc les mêmes TYPES : tout en chaînes, parce que
    c'est ce que bsx.read_items rend en lisant le XML. Un color_id entier ici et le pin ne
    retomberait sur rien, silencieusement."""
    return (str(item_id), str(color_id), CONDITION)


def copies_of(set_no):
    """Exemplaires possédés de ce set (le ×N), depuis la table `inventory` de l'historique —
    la même source que plan.set_multiplier_of, mais par NUMÉRO : un set acheté mais pas
    encore arrivé n'a pas de dossier."""
    from . import history
    h = history.HistoryDB()
    try:
        return h.inventory_qty(str(set_no)) or 0
    finally:
        h.close()


def virtual_set(set_no, copies, cat=None, wdb=None, with_categories=True):
    """Les lots d'un set tels que le catalogue les connaît, à l'échelle ×N.

    Rend (meta, qty, poids, infos) avec exactement la forme que build_plan manipule, pour que
    la Phase A ne sache pas de quelle source elle vient."""
    from . import catalogdb
    if cat is None:
        cat = catalogdb.shared()
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()
    if not cat.has_set(str(set_no)):
        raise KeyError("set %s absent du catalogue BrickStore" % set_no)

    meta, qty, weight = {}, {}, {}
    no_weight, no_category = [], []
    for part in cat.inventory(str(set_no), include_extras=True):
        k = _key(part.item_id, part.color_id)
        if k not in meta:
            category = ""
            if with_categories:
                from . import categories
                category = categories.category_of(part.item_id)[1] or ""
            if not category:
                no_category.append(k)
            meta[k] = {"item_id": str(part.item_id), "color_id": str(part.color_id),
                       "condition": CONDITION, "item_type": part.item_type,
                       "item_name": part.item_name, "color_name": part.color_name,
                       "category_name": category, "qty": 0}
            w = wdb.weight(str(part.item_id), part.item_name)
            if w is None:
                no_weight.append(k)
            weight[k] = w or 0.0
        qty[k] = qty.get(k, 0) + part.qty * max(1, int(copies or 1))

    info = cat.set_info(str(set_no))
    return meta, qty, weight, {
        "set": str(set_no), "name": info.name if info else "", "copies": int(copies or 0),
        "lots": len(meta), "pieces": sum(qty.values()),
        "no_weight": len(no_weight), "no_category": len(no_category),
    }


def compute(sets, cat=None, wdb=None):
    """La Phase A sur l'union des sets prévus.

    `sets` : [numéro, ...] ou [(numéro, exemplaires), ...]. Sans exemplaires, ils sont lus
    dans la table `inventory` — un set sans achat enregistré est rendu à 0 et signalé, parce
    qu'un ×N faux fausse tous les poids et donc toute la numérotation.
    """
    from . import catalogdb
    if cat is None:
        cat = catalogdb.shared()
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()

    meta, total_qty, weight_g = {}, {}, {}
    rows, unknown = [], []
    for entry in sets:
        set_no, copies = entry if isinstance(entry, (tuple, list)) else (entry, None)
        if copies is None:
            copies = copies_of(set_no)
        try:
            m, q, w, info = virtual_set(set_no, copies, cat=cat, wdb=wdb)
        except KeyError as e:
            unknown.append(str(set_no))
            rows.append({"set": str(set_no), "error": str(e)})
            continue
        for k, it in m.items():
            meta.setdefault(k, it)          # un moule vu dans deux sets reste UN lot
            weight_g.setdefault(k, w[k])
        for k, n in q.items():
            total_qty[k] = total_qty.get(k, 0) + n
        rows.append(info)

    sac_of, num_sacs = flow_sacs(meta, total_qty, weight_g)

    # La PASSE DE CONSOLIDATION, elle, est calculable d'avance : elle ne demande que « quelles
    # couleurs d'un même moule finissent dans le même Sac », et Phase A vient de le décider.
    # (La Phase B, non — voir la note en tête de module.)
    try:
        from . import batch as _batch
        conso = _batch.consolidation_pass({"sac_of": sac_of, "meta": meta})
    except Exception:
        conso = []

    grams = {}
    for k, s in sac_of.items():
        grams[s] = round(grams.get(s, 0.0) + weight_g.get(k, 0.0) * total_qty.get(k, 0), 1)
    moulds = {}
    for k in meta:
        moulds.setdefault(mould_key(k), set()).add(sac_of.get(k))

    stats = {
        "sets": rows,
        "unknown_sets": unknown,
        "no_copies": [r["set"] for r in rows if not r.get("error") and not r.get("copies")],
        "lots": len(meta),
        "pieces": sum(total_qty.values()),
        "weight_g": round(sum(weight_g.get(k, 0.0) * total_qty.get(k, 0) for k in meta), 1),
        "sacs": num_sacs,
        "sac_grams": dict(sorted(grams.items())),
        "overweight_sacs": sorted(s for s, g in grams.items() if g > config.SAC_TARGET_GRAMS),
        "no_weight": sum(r.get("no_weight", 0) for r in rows),
        "no_category": sum(r.get("no_category", 0) for r in rows),
        "split_moulds": sorted(m[0] for m, s in moulds.items() if len(s) > 1),
        "sous_sacs": sum(len(x["sous_sacs"]) for x in conso),
        "sacs_avec_sous_sacs": sum(1 for x in conso if x["sous_sacs"]),
        "pire_sac": max(((len(x["sous_sacs"]), x["sac"]) for x in conso), default=(0, 0)),
        "target_pieces": getattr(config, "BATCH_TARGET_PIECES", 20000),
    }
    return {"layout": sac_of, "stats": stats}


# --- ranger / relire -----------------------------------------------------------------

def to_json(result, name=None):
    """La clé d'un lot est un triplet : on la range en LISTE, pas en chaîne collée — un
    ItemID peut contenir à peu près n'importe quoi, un séparateur finirait par tomber
    dessus."""
    return {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "batch": name,
        "stats": result["stats"],
        "layout": [{"k": list(k), "sac": s} for k, s in sorted(result["layout"].items())],
    }


def save(result, path, name=None):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_json(result, name), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)               # jamais de fichier à moitié écrit sur Drive
    return path


def load(path):
    """{clé -> Sac} + ce qui va avec, ou None si le batch n'a pas de plan prévisionnel."""
    if not os.path.exists(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    layout = {}
    for row in data.get("layout") or []:
        k = row.get("k") or []
        if len(k) == 3:
            layout[(str(k[0]), str(k[1]), str(k[2]))] = int(row.get("sac") or 0)
    data["layout"] = layout
    return data


def verify_composition(data, sets):
    """Le plan vaut-il encore ? Rend la liste des écarts entre la composition qu'il a servie
    et celle d'aujourd'hui (set ajouté, retiré, ou nombre d'exemplaires changé). Vide = bon.

    C'est LE contrôle qui compte : tout ce qui est déjà trié suppose cette composition."""
    was = {str(r.get("set")): int(r.get("copies") or 0)
           for r in ((data or {}).get("stats") or {}).get("sets", []) if not r.get("error")}
    now = {}
    for entry in sets:
        set_no, copies = entry if isinstance(entry, (tuple, list)) else (entry, None)
        now[str(set_no)] = int((copies if copies is not None else copies_of(set_no)) or 0)
    out = []
    for s in sorted(set(was) | set(now)):
        if s not in now:
            out.append("%s n'est plus au batch" % s)
        elif s not in was:
            out.append("%s a rejoint le batch après le plan" % s)
        elif was[s] != now[s]:
            out.append("%s : ×%d au plan, ×%d aujourd'hui" % (s, was[s], now[s]))
    return out


def report(result, log=print):
    """Ce qu'il faut lire avant de figer un plan — et surtout ce qui cloche."""
    st = result["stats"]
    log("Plan prévisionnel : %d lots, %d pièces, %.0f g → %d Sacs"
        % (st["lots"], st["pieces"], st["weight_g"], st["sacs"]))
    if st.get("sous_sacs"):
        n, sac = st.get("pire_sac") or (0, 0)
        log("  passe de consolidation : %d sous-sacs sur %d Sacs (le pire : Sac %02d, %d)"
            % (st["sous_sacs"], st["sacs_avec_sous_sacs"], sac, n))
    for r in st["sets"]:
        if r.get("error"):
            log("  %-8s ABSENT DU CATALOGUE : %s" % (r["set"], r["error"]))
        else:
            log("  %-8s ×%-3d %4d lots %7d pièces  %s"
                % (r["set"], r["copies"], r["lots"], r["pieces"], r["name"][:40]))
    if st["no_copies"]:
        log("  ⚠ aucun achat enregistré (table inventory) pour : %s — le ×N vaut 0, "
            "logue l'achat au Journal avant de figer le plan" % ", ".join(st["no_copies"]))
    if st["pieces"] < st["target_pieces"]:
        log("  ⚠ %d pièces pour une cible de %d : l'envoi n'est pas complet"
            % (st["pieces"], st["target_pieces"]))
    if st["no_category"]:
        log("  %d lot(s) sans catégorie connue — ils se trieront autrement une fois "
            "catalogués (sortpack.categories)" % st["no_category"])
    if st["no_weight"]:
        log("  %d lot(s) sans poids au catalogue (comptés 0 g)" % st["no_weight"])
    if st["overweight_sacs"]:
        log("  Sacs au-dessus de %.0f g : %s"
            % (config.SAC_TARGET_GRAMS, ", ".join(str(s) for s in st["overweight_sacs"])))
    return result
