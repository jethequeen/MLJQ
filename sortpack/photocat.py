# -*- coding: utf-8 -*-
"""Cataloguage par photo — le téléphone reconnaît, le PC écrit les .bsx.

Cataloguer à la main veut dire décrémenter l'`Inventory*.bsx` et incrémenter un sac
numéroté (voir *Vérification des quantités* au README). Ce module fait ce geste à partir
de ce que le téléphone a reconnu sur une photo, et rien de plus : il ne planifie pas, il
n'écrit aucune remarque. L'auto-apply s'en charge tout seul au passage suivant — il décide
sur l'empreinte des FICHIERS, donc nos écritures le réveillent sans qu'on le lui dise.

Deux fichiers par set, dans le dossier d'état que le téléphone lit déjà :

* ``catalogue_<set>.json`` — écrit ICI, lu par le téléphone : ce que l'inventaire contient
  ENCORE, lot par lot, plus les noms de sacs proposés. C'est lui qui permet au téléphone de
  contraindre la reconnaissance à ce que la boîte contient vraiment, sans aller-retour :
  parmi les candidats que rend Brickognize, le bon est celui qui est encore à l'inventaire.
* ``catsession_<set>.json`` — écrit par le TÉLÉPHONE après chaque photo, lu ici à la
  fermeture. Le navigateur n'est pas un endroit où ranger une heure de saisie : le
  sous-domaine d'Apps Script tourne, et le `localStorage` s'évapore avec lui.

Pourquoi l'écriture n'arrive qu'à « Fermer la liste » : un `.bsx` vide n'est pas un sac.
`plan.discover_bags` saute les fichiers sans lot, mais `gui.bag_count` compte les NOMS —
un `4.bsx` créé d'avance ferait apparaître un quatrième sac qui ne contient rien. Une
session abandonnée ne laisse donc aucun fichier derrière elle.
"""

import os
import re
import json
import glob
import datetime
from collections import defaultdict

from . import config
from . import bsx
from . import plan as plan_mod


# Un nom de sac : "3", ou "3 - 1" pour la deuxième liste du même sac. C'est la forme que
# `plan.bag_sort_key` sait ordonner ; tout le reste sortirait de l'ordre des sacs.
_LABEL_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def _key(it):
    return f'{it["item_id"]}|{it["color_id"]}|{it["condition"]}'


def normalize_label(label):
    """« 3-1 », « 3 - 1 », « 3 » → « 3 - 1 » / « 3 ». Lève pour tout le reste."""
    m = _LABEL_RE.match(str(label or ""))
    if not m:
        raise ValueError(f"nom de sac invalide : {label!r} — attendu « 3 » ou « 3 - 1 »")
    main, sub = int(m.group(1)), m.group(2)
    return f"{main} - {int(sub)}" if sub is not None else str(main)


def bag_labels(set_folder):
    """Les sacs qui existent déjà, dans l'ordre des sacs."""
    out = []
    for path in glob.glob(os.path.join(set_folder, "*.bsx")):
        base = os.path.basename(path)
        if any(base.startswith(p) for p in config.SKIP_PREFIXES):
            continue
        out.append(base[:-4])
    out.sort(key=plan_mod.bag_sort_key)
    return out


def next_bags(labels):
    """Les deux noms à proposer : la suite du sac en cours, et le sac suivant.

    Avec 1, 1-1, 2, 2-1, 3 en place, ce sont « 3 - 1 » et « 4 » — on ne peut pas deviner
    lequel des deux, parce que ça dépend d'un fait physique (le sac 3 est-il vide ?) que
    les fichiers ne portent pas. Le téléphone montre donc les deux.
    """
    biggest = sub = 0
    for label in labels:
        kind, main, s, _ = plan_mod.bag_sort_key(label)
        if kind != 0:
            continue
        if main > biggest:
            biggest, sub = main, s
        elif main == biggest and s > sub:
            sub = s
    if not biggest:
        return ["1"]
    return [f"{biggest} - {sub + 1}", str(biggest + 1)]


def _fingerprint(set_folder):
    """Ce qui doit changer pour que l'instantané soit refait. Sur les stat() seuls : il est
    relu à chaque poll, pour tous les sets en cataloguage — ouvrir les XML à chaque passage
    coûterait une seconde de disque Drive pour, presque toujours, rien."""
    parts = []
    try:
        names = sorted(os.listdir(set_folder))
    except OSError:
        return ""
    for name in names:
        if not name.lower().endswith(".bsx"):
            continue
        try:
            st = os.stat(os.path.join(set_folder, name))
        except OSError:
            continue
        parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


def _totals(set_folder):
    """Pièces par (lot, couleur, état) dans TOUS les .bsx du set. Le seul invariant qu'une
    inscription ne doit jamais casser — on ne crée rien, on déplace."""
    out = defaultdict(int)
    for name in sorted(os.listdir(set_folder)):
        if not name.lower().endswith(".bsx"):
            continue
        for it in bsx.read_items(os.path.join(set_folder, name)):
            out[_key(it)] += it["qty"]
    return dict(out)


def _inventory_scale(inv_path):
    """Le facteur dont l'inventaire porte déjà la marque, ou 1.

    Le cataloguage travaille à l'échelle de BASE — on sort les pièces d'un exemplaire, à la
    main — et `verify.normalize_inventory` ne passe le fichier à ×N qu'à l'Apply. Décrémenter
    un inventaire déjà multiplié des quantités d'un seul exemplaire laisserait N−1 copies de
    chaque pièce dedans, que le maître enverrait au client.
    """
    try:
        with open(inv_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return 1
    return bsx.multiplier_stamp(raw) or 1


def _currency(path):
    """La devise du fichier d'où les lots sortent, pour que le sac naisse dans la même."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = re.search(r'<Inventory\b[^>]*\bCurrency="([^"]+)"', f.read())
        return m.group(1) if m else "CAD"
    except OSError:
        return "CAD"


def snapshot(set_folder):
    """Ce que le téléphone a besoin de savoir du set pour cataloguer en photo."""
    inv_path = plan_mod.find_inventory(set_folder)
    labels = bag_labels(set_folder)
    lots, inv_qty = [], 0
    if inv_path:
        for it in bsx.read_items(inv_path):
            if it["qty"] <= 0:
                continue
            inv_qty += it["qty"]
            lots.append({
                "k": _key(it), "id": it["item_id"], "type": it["item_type"] or "P",
                "color": it["color_id"], "color_name": it["color_name"],
                "name": it["item_name"], "qty": it["qty"],
            })
    bags_qty = sum(bsx.total_qty(os.path.join(set_folder, f"{b}.bsx")) for b in labels)
    base = inv_qty + bags_qty
    # Un même lot peut tenir deux lignes de l'inventaire (la même pièce ajoutée deux fois) :
    # le téléphone raisonne par clé, donc on regroupe ici plutôt que de le lui faire faire.
    merged = {}
    for lot in lots:
        if lot["k"] in merged:
            merged[lot["k"]]["qty"] += lot["qty"]
        else:
            merged[lot["k"]] = lot
    lots = sorted(merged.values(), key=lambda l: (l["name"] or "", l["k"]))
    return {
        "set": plan_mod.set_number(set_folder) or "",
        "folder_name": os.path.basename(set_folder.rstrip("\\/")),
        "written": datetime.datetime.now().isoformat(timespec="seconds"),
        "fp": _fingerprint(set_folder),
        "inventory": os.path.basename(inv_path) if inv_path else "",
        "scale": _inventory_scale(inv_path) if inv_path else 1,
        "inv_qty": inv_qty, "bags_qty": bags_qty,
        "left_fraction": (inv_qty / base) if base else 0.0,
        "done_fraction": config.CATALOGUE_DONE_FRACTION,
        "bags": labels, "next_bags": next_bags(labels),
        "lots": lots,
    }


def snapshot_path(set_no):
    return config.state_path(config.CATALOGUE_FILE.format(set=set_no))


def sessions_path():
    return config.state_path(config.CATALOGUE_SESSIONS_FILE)


def write_snapshot(set_folder, data=None):
    """Écrit `catalogue_<set>.json`. Best-effort : Drive peut tenir le fichier."""
    data = data or snapshot(set_folder)
    if not data.get("set"):
        return None
    path = snapshot_path(data["set"])
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return path
    except OSError:
        return None


def maybe_write_snapshot(set_folder, log=None):
    """Le réécrire seulement si les .bsx ont bougé. L'empreinte voyage DANS le fichier :
    un état de plus à côté serait un fichier de plus à garder cohérent sur Drive."""
    set_no = plan_mod.set_number(set_folder)
    if not set_no:
        return False
    fp = _fingerprint(set_folder)
    path = snapshot_path(set_no)
    try:
        with open(path, "r", encoding="utf-8") as f:
            if json.load(f).get("fp") == fp:
                return False
    except (OSError, ValueError):
        pass
    data = snapshot(set_folder)
    if write_snapshot(set_folder, data) and log:
        log(f"  inventaire du {set_no} publié pour le téléphone "
            f"({len(data['lots'])} lots, {data['inv_qty']} pièces)")
    return True


def refresh_all(sets, log=None):
    """Republier l'inventaire des sets EN COURS DE CATALOGUAGE, et nettoyer les autres.

    `sets` est l'index que le bureau écrit déjà pour le téléphone (gui.build_index), donc
    on ne balaie pas le disque une deuxième fois. On s'accroche là parce que c'est le seul
    endroit par lequel passent les trois chemins qui tiennent les fichiers du téléphone à
    jour : le rafraîchissement du GUI, son poll, et la tâche planifiée.

    Un set qui n'est plus en cataloguage voit son instantané SUPPRIMÉ : il n'y a plus de
    photo à prendre, et un fichier resté là laisserait le téléphone proposer d'ajouter un
    sac à un set qu'on est en train de trier. Retourne le nombre de fichiers réécrits.
    """
    n = 0
    for s in sets or []:
        folder = s.get("folder")
        if not folder or not os.path.isdir(folder):
            continue
        if s.get("cataloguing") and s.get("has_inventory"):
            try:
                if maybe_write_snapshot(folder, log=log):
                    n += 1
            except Exception as e:
                if log:
                    log(f"  ⚠ inventaire du {s.get('number')} non publié ({str(e)[:60]})")
        else:
            try:
                os.remove(snapshot_path(s.get("number") or ""))
            except OSError:
                pass
    return n


# Les listes SOUMISES et pas encore inscrites vivent sous cette clé, à part des listes
# ouvertes. Clé interne et non un numéro de set : un set s'appelle par des chiffres.
SENT_KEY = "_sent"


def read_sessions():
    """Le fichier entier : {numéro de set: liste ouverte} + "_sent". Le téléphone l'écrit."""
    try:
        with open(sessions_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_sessions(data):
    try:
        with open(sessions_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def read_session(set_no):
    """La liste OUVERTE d'un set — celle qu'on est en train de remplir."""
    return read_sessions().get(str(set_no))


def read_sent(set_no, bag):
    """Une liste FERMÉE qui attend encore son inscription.

    Elle est rangée à part de la liste ouverte, et c'est tout l'intérêt : fermer un sac ne
    bloque plus l'écran, donc le téléphone en ouvre aussitôt un autre POUR LE MÊME SET. Les
    deux étant jadis rangées sous la même clé, la nouvelle écrasait celle qui attendait, et
    le PC refusait ensuite d'inscrire un sac dont il ne retrouvait plus la liste — une heure
    de saisie perdue sans un mot.
    """
    sent = read_sessions().get(SENT_KEY) or {}
    return sent.get("%s|%s" % (set_no, normalize_label(bag))) if bag else None


def clear_sent(set_no, bag):
    """Oublier une liste fermée une fois inscrite."""
    data = read_sessions()
    sent = data.get(SENT_KEY) or {}
    k = "%s|%s" % (set_no, normalize_label(bag))
    if k not in sent:
        return False
    del sent[k]
    data[SENT_KEY] = sent
    return _write_sessions(data)


def clear_session(set_no):
    """Oublier la liste ouverte d'un set, les autres intactes. Relu juste avant d'écrire : le
    téléphone peut avoir ajouté une photo à un AUTRE set entre-temps."""
    data = read_sessions()
    if str(set_no) not in data:
        return False
    del data[str(set_no)]
    return _write_sessions(data)


class _BagLot(object):
    """De quoi écrire un <Item> de sac, copié du lot d'inventaire d'où il sort.

    La catégorie est recopiée, et comblée quand elle manque : `bsx.write_inventory` n'en
    écrit pas (BrickStore la remplit à l'ouverture), donc un inventaire jamais ouvert n'en a
    aucune — et la Phase A trie SUR la catégorie. Un lot arrivé sans elle se trie avant tout
    le reste et part dans le premier Sac au lieu d'être emballé avec les siens.
    """
    __slots__ = ("item_id", "item_type", "color_id", "color_name", "item_name", "qty",
                 "category_id", "category_name")

    def __init__(self, it, qty):
        self.item_id, self.item_type = it["item_id"], it["item_type"] or "P"
        self.color_id, self.color_name = it["color_id"], it["color_name"]
        self.item_name, self.qty = it["item_name"], qty
        self.category_id = it.get("category_id") or ""
        self.category_name = it.get("category_name") or ""
        if not self.category_name:
            try:
                from . import categories
                self.category_id, self.category_name = categories.category_of(it["item_id"])
            except Exception:
                pass


def apply_session(set_folder, bag_label, lots, backup=None, log=None):
    """Inscrire au sac `bag_label` ce que la session a reconnu, en le sortant de l'inventaire.

    `lots` : [{"k": "3001|5|N", "qty": 2}, …] — ou "id"/"color" au lieu de la clé. Les
    quantités sont à l'échelle de BASE (un exemplaire), comme le reste du cataloguage.

    Ce qu'on ne peut pas prendre est RAPPORTÉ, pas inventé : si l'inventaire n'a plus la
    pièce, c'est l'inventaire qui a tort (`verify` existe pour ça), mais l'inscrire quand
    même casserait l'invariant des totaux. On inscrit donc ce que l'inventaire tient, et le
    manque remonte au téléphone lot par lot — le chemin normal ne le voit jamais, puisque le
    téléphone ne laisse ajouter que ce que l'instantané contient.
    """
    from . import apply as apply_mod
    if backup is None:
        backup = config.ENABLE_BACKUP
    label = normalize_label(bag_label)
    inv_path = plan_mod.find_inventory(set_folder)
    if not inv_path:
        raise RuntimeError("ce set n'a pas d'Inventory*.bsx — rien à décrémenter")
    scale = _inventory_scale(inv_path)
    if scale > 1:
        raise RuntimeError(f"l'inventaire est déjà multiplié ×{scale} : le cataloguage par "
                           "photo travaille à l'échelle de base")

    dest_path = os.path.join(set_folder, f"{label}.bsx")
    if os.path.exists(dest_path) and bsx.has_remarks(dest_path):
        raise RuntimeError(f"le sac {label} porte déjà des remarques — il est planifié, donc "
                           "passe par « j'ai cette pièce en main » plutôt que par ici")

    by_key = defaultdict(list)
    for it in bsx.read_items(inv_path):
        if it["qty"] > 0:
            by_key[_key(it)].append(it)

    # Ce qu'on demande, regroupé par clé : deux photos peuvent avoir trouvé la même pièce.
    want = defaultdict(int)
    for lot in lots:
        k = str(lot.get("k") or "").strip()
        if not k:
            k = f'{lot.get("id")}|{lot.get("color")}|{lot.get("condition") or "N"}'
        q = int(lot.get("qty") or 0)
        if q > 0:
            want[k] += q
    if not want:
        raise RuntimeError("session vide — rien à inscrire")

    before = _totals(set_folder)
    touched = [inv_path] + ([dest_path] if os.path.exists(dest_path) else [])
    backup_dir = apply_mod.backup_files(set_folder, touched) if backup else None

    dest_rows = {}
    if os.path.exists(dest_path):
        for it in bsx.read_items(dest_path):
            dest_rows.setdefault(_key(it), it)

    qty_by_row, remove_rows, bumps, append, shorts = {}, [], {}, [], []
    written = pieces = 0
    for k, q in sorted(want.items()):
        left, src = q, None
        for it in by_key.get(k, []):
            if left <= 0:
                break
            cut = min(it["qty"], left)
            left -= cut
            src = src or it
            rest = it["qty"] - cut
            if rest <= 0:
                remove_rows.append(it["row"])
            else:
                qty_by_row[it["row"]] = rest
        take = q - left
        if left > 0:
            shorts.append({"k": k, "asked": q, "written": take,
                           "name": (src or {}).get("item_name", "")})
        if take <= 0:
            continue
        if k in dest_rows:
            row = dest_rows[k]
            bumps[row["row"]] = row["qty"] + take
        else:
            append.append(_BagLot(src, take))
        written += 1
        pieces += take

    if not written:
        raise RuntimeError("aucune de ces pièces n'est encore à l'inventaire — "
                           "lance « Vérifier les quantités » sur ce set")

    bsx.adjust_items(inv_path, qty_by_row=qty_by_row, remove_rows=remove_rows)
    if not os.path.exists(dest_path):
        bsx.new_document(dest_path, currency=_currency(inv_path))
    if bumps:
        bsx.adjust_items(dest_path, qty_by_row=bumps)
    if append:
        bsx.append_lots(dest_path, [(lot, lot.qty) for lot in append])

    after = _totals(set_folder)
    if after != before:
        raise RuntimeError("ANNULÉ : l'inscription aurait changé les quantités — "
                           f"restaure {backup_dir or 'la sauvegarde'}")

    data = snapshot(set_folder)
    write_snapshot(set_folder, data)
    if log:
        log(f"Sac {label} : {written} lots, {pieces} pièces "
            f"(inventaire : {data['inv_qty']} pièces, {data['left_fraction'] * 100:.0f} %)")
    return {"bag": label, "path": dest_path, "lots": written, "pieces": pieces,
            "shorts": shorts, "backup_dir": backup_dir,
            "left_fraction": data["left_fraction"], "inv_qty": data["inv_qty"],
            "catalogue_done": data["left_fraction"] <= config.CATALOGUE_DONE_FRACTION}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Cataloguage par photo (côté PC)")
    ap.add_argument("folder", help="le dossier du set")
    ap.add_argument("--apply", metavar="JSON",
                    help="inscrire une session : "
                         "{\"bag\":\"4\",\"lots\":[{\"k\":\"3001|5|N\",\"qty\":2}]}")
    a = ap.parse_args()
    if a.apply:
        payload = json.loads(a.apply)
        res = apply_session(a.folder, payload["bag"], payload.get("lots") or [], log=print)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    data = snapshot(a.folder)
    print(f"{data['folder_name']} — inventaire {data['inv_qty']} pièces "
          f"({data['left_fraction'] * 100:.1f} %), sacs {', '.join(data['bags']) or '—'}")
    print(f"à proposer : {' ou '.join(data['next_bags'])}")
    print(f"{len(data['lots'])} lots encore à cataloguer")


if __name__ == "__main__":
    main()
