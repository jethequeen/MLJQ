# -*- coding: utf-8 -*-
"""
Les BATCHS — plusieurs sets traités comme un seul envoi.

Le client range son inventaire **par passes** : il longe ses bacs dans l'ordre du catalogue
en vidant les sacs. Deux envois séparés, c'est deux parcours complets. Il demande donc des
lots de ~20 000 pièces, soit environ quatre sets, en **une** liste et **une** séquence de
sacs.

Le point essentiel du design : un batch se déclare **avant** le tri. On attend que ses sets
soient catalogués, puis on planifie la Phase A sur leur union — même tri, mêmes 650 g, même
cycle C/D, mais une seule numérotation de Sacs sur tout l'envoi. Il n'y a jamais deux plans,
donc rien à fusionner ensuite. Trier d'abord set par set puis réunir coûtait ~372
manipulations par batch (mesuré sur 43011+43027+76342+77256) ; ici, zéro.

Ce que ça change physiquement pendant le tri :

* les Sacs du batch restent **ouverts** — leur nombre est connu d'avance (24 pour l'exemple
  ci-dessus), et une couleur terminée y tombe directement, sans recherche ;
* les boîtes **C** restent nécessaires : elles rassemblent une couleur croisée dans plusieurs
  fichiers, faute de quoi on créerait deux lots de la même (pièce, couleur) ;
* les boîtes **D** disparaissent. Phase A place déjà toutes les couleurs d'un moule dans le
  MÊME Sac, donc elles s'y retrouvent d'elles-mêmes. Le regroupement en sous-sac, que le
  client exige, se fait en fin de batch par une passe **locale à chaque Sac** : on n'y
  cherche jamais rien ailleurs (vérifié : 0 moule réparti sur deux Sacs, sauf ceux qui pèsent
  plus qu'un Sac, que Phase A scinde volontairement).

Le flux par set n'est pas touché : un set hors batch se planifie, s'applique et se finalise
exactement comme avant.

    python -m sortpack.batch list
    python -m sortpack.batch create envoi-01 43011 43027 76342 77256
    python -m sortpack.batch add envoi-01 11503
    python -m sortpack.batch plan envoi-01
    python -m sortpack.batch apply envoi-01
    python -m sortpack.batch cfb envoi-01
"""

import os
import re
import json
import shutil
import datetime

from . import config
from . import bsx
from .plan import (build_plan, discover_bags, find_inventory, is_set_folder, set_number,
                   mould_key, iter_set_folders, batch_root)

BATCH_FILE = os.path.join(config.CATALOGUAGE_ROOT, "batches.json")

# Ce que le client demande par envoi. Un batch est « prêt à fermer » une fois atteint.
TARGET_PIECES = getattr(config, "BATCH_TARGET_PIECES", 20000)


# --- le registre --------------------------------------------------------------

def _read():
    try:
        with open(BATCH_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    tmp = BATCH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BATCH_FILE)


def all_batches():
    """{nom: {"sets": [...], "created": ..., "closed": bool}}"""
    return _read()


def get(name):
    b = _read().get(name)
    if b is None:
        raise KeyError(f"batch « {name} » inconnu")
    return b


def batch_dir(name, create_it=False):
    """Le dossier du batch : CFB/Batchs/<nom>. Il contient le fichier de consolidation, le
    maître une fois construit, et les dossiers de sets du batch."""
    d = os.path.join(batch_root(), str(name))
    if create_it:
        os.makedirs(d, exist_ok=True)
    return d


def create(name, sets=()):
    d = _read()
    if name in d:
        raise RuntimeError(f"le batch « {name} » existe déjà")
    batch_dir(name, create_it=True)
    d[name] = {"sets": [],
               "created": datetime.datetime.now().isoformat(timespec="seconds"),
               "closed": False}
    _write(d)
    for s in sets:
        add(name, s)
    return _read()[name]


def add(name, set_no):
    d = _read()
    b = d.get(name)
    if b is None:
        raise KeyError(f"batch « {name} » inconnu")
    if b.get("closed"):
        raise RuntimeError(f"le batch « {name} » est fermé")
    s = str(set_no)
    for other, ob in d.items():
        if other != name and s in ob.get("sets", []) and not ob.get("closed"):
            raise RuntimeError(f"le set {s} est déjà dans le batch « {other} »")
    if s not in b["sets"]:
        b["sets"].append(s)
    _write(d)
    _move_into(name, s)
    return b


def _rename(src, dest, tries=5, delay=0.6):
    """Déplacer un dossier de set, par renommage SEULEMENT.

    `shutil.move` n'est pas sûr ici : si le renommage échoue (Google Drive ou l'explorateur
    tient le dossier ouvert), il retombe sur copier-puis-supprimer, et si la suppression
    échoue à son tour on se retrouve avec le contenu des deux côtés. `os.rename` est atomique
    sur un même volume — ou il réussit, ou rien n'a bougé. On réessaie quelques fois, parce
    que le verrou de Drive est le plus souvent passager, puis on rend la main avec un message
    qui dit quoi fermer."""
    import time
    if os.path.exists(dest):
        raise RuntimeError(f"« {dest} » existe déjà — déplace ou renomme-le à la main")
    last = None
    for i in range(tries):
        try:
            os.rename(src, dest)
            return dest
        except OSError as e:
            last = e
            time.sleep(delay)
    raise RuntimeError(
        f"impossible de déplacer « {os.path.basename(src)} » : {last}.\n"
        "Ferme le dossier dans l'explorateur et les fichiers ouverts dans BrickStore, "
        "laisse Google Drive finir sa synchronisation, puis réessaie. "
        "Rien n'a été modifié.")


def _move_into(name, set_no):
    """Déplace le dossier du set dans celui du batch. Le batch POSSÈDE ses sets : c'est ce
    déplacement, et non une entrée dans un fichier, qui fait foi — on retrouve toujours un
    set en balayant (plan.iter_set_folders), même si le registre est perdu.

    Le déplacement est IMMÉDIAT, cataloguage fini ou non. On attendait la fin du cataloguage,
    au motif qu'on catalogue là où on en a l'habitude ; en pratique ça laissait un set de
    l'envoi à la racine pendant des jours (le Jaguar de l'envoi 1, créé là avec son
    inventaire), donc à part dans la liste du GUI, à part dans l'explorateur, et sans rien
    qui dise à quel envoi il appartient. Le dossier du batch EST l'endroit où on en a
    l'habitude dès qu'un set y est inscrit."""
    src = folder_of(set_no)
    if src is None:
        return None                      # pas encore de dossier : rien à déplacer
    dest_dir = batch_dir(name, create_it=True)
    if os.path.abspath(os.path.dirname(src)) == os.path.abspath(dest_dir):
        return src                       # déjà dedans
    return _rename(src, os.path.join(dest_dir, os.path.basename(src)))


def _move_out(set_no):
    """Ramène le dossier du set à la racine du cataloguage."""
    src = folder_of(set_no)
    if src is None:
        return None
    root = config.CATALOGUAGE_ROOT
    if os.path.abspath(os.path.dirname(src)) == os.path.abspath(root):
        return src
    return _rename(src, os.path.join(root, os.path.basename(src)))


def remove(name, set_no):
    d = _read()
    b = d.get(name)
    if b is None:
        raise KeyError(f"batch « {name} » inconnu")
    b["sets"] = [s for s in b["sets"] if s != str(set_no)]
    _write(d)
    _move_out(set_no)                    # il repart à la racine, traitable seul
    return b


def settle(log=None):
    """Emporte dans leur batch les dossiers des sets inscrits qui n'y sont pas encore.

    Un set acheté pour un envoi y est inscrit dès la synchronisation des achats, parfois
    avant que son dossier existe (le colis n'est pas arrivé). C'est ici qu'il rejoint le
    batch dès que le dossier apparaît — `purchases._folder_for` le crée déjà directement
    dedans quand l'inscription précède la création, donc il ne reste ici que les retards :
    un set inscrit à la main après coup, un dossier recréé, un déplacement qu'un verrou de
    Drive avait fait échouer. Appelé avant toute opération de batch et par la passe de fond,
    donc on n'a jamais à y penser. Renvoie la liste des sets déplacés."""
    moved = []
    for name, b in _read().items():
        # Fermé ne veut pas dire fini : c'est justement une fois l'envoi fermé qu'on le
        # trie, et un set peut n'arriver qu'à ce moment-là. Seul un envoi SOLDÉ n'a plus
        # rien à réclamer — ses dossiers sont à la corbeille.
        if is_finished(b):
            continue
        for s in list(b.get("sets", [])):
            f = folder_of(s)
            if f is None:
                continue
            if os.path.abspath(os.path.dirname(f)) == os.path.abspath(batch_dir(name)):
                continue                 # déjà dedans
            try:
                if _move_into(name, s):
                    moved.append(s)
                    if log:
                        log(f"  {s} rejoint le dossier du batch « {name} »")
            except Exception as e:
                if log:
                    log(f"  ⚠ {s} non déplacé : {str(e)[:70]}")
    return moved


def pending(name):
    """Les sets inscrits au batch dont le dossier n'y est pas encore (déplacement en
    attente : dossier tenu ouvert par Drive ou par l'explorateur)."""
    out = []
    for s in get(name).get("sets", []):
        f = folder_of(s)
        if f and os.path.abspath(os.path.dirname(f)) != os.path.abspath(batch_dir(name)):
            out.append(s)
    return out


def close(name, closed=True):
    d = _read()
    d[name]["closed"] = bool(closed)
    if not closed:
        d[name].pop("finished", None)    # rouvrir, c'est annuler le solde : il reprend ses sets
    _write(d)
    return d[name]


def _mark_finished(name):
    """Solde l'envoi : fermé ET fini. Voir `is_finished`."""
    d = _read()
    d[name]["closed"] = True
    d[name]["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write(d)
    return d[name]


def batch_of(set_no):
    """Le batch OUVERT qui contient ce set, ou None. Sert là où la question est « cet envoi
    peut-il encore changer de composition ? » — l'inscription d'un achat, la cible de la
    liste d'achat."""
    for name, b in _read().items():
        if not b.get("closed") and str(set_no) in b.get("sets", []):
            return name
    return None


def is_finished(b):
    """Cet envoi est-il SOLDÉ ? (`finish()` : stock retiré, dossiers à la corbeille.)

    Un envoi fermé possède encore ses sets ; un envoi soldé n'a plus rien — il ne reste que
    son maître et sa consolidation. La nuance compte parce qu'un set se rachète : sans elle,
    le dossier d'un rachat irait naître dans l'envoi parti il y a six mois."""
    return bool(b.get("finished"))


def owning_batch(set_no):
    """Le batch qui POSSÈDE ce set, fermé compris (l'ouvert l'emporte), ou None.

    Fermer un envoi veut dire « plus rien n'entre », pas « ce set n'est plus à lui » : ses
    sets lui appartiennent jusqu'à `finish()`, qui jette leurs dossiers. Partout où la
    question est « où vit son dossier ? » ou « qui a le droit de le planifier ? », c'est
    CETTE réponse qu'il faut — `batch_of` seul laissait un set d'envoi fermé se faire
    renuméroter tout seul par l'auto-apply par set. Un envoi SOLDÉ, lui, ne possède plus
    rien : il ne réclame pas le dossier d'un rachat."""
    s = str(set_no)
    fallback = None
    for name, b in _read().items():
        if s not in [str(x) for x in b.get("sets", [])] or is_finished(b):
            continue
        if not b.get("closed"):
            return name
        fallback = fallback or name
    return fallback


# --- cataloguage en cours ------------------------------------------------------

def _catalogue_done(folder):
    """Le cataloguage de ce dossier est-il fini ? (délégué à `remote`, qui en décide pour
    tout le projet : la question ne doit avoir qu'une seule réponse.)"""
    try:
        from . import remote as _remote
        return _remote._catalogue_done(folder)
    except Exception:
        return False


def bag_total(folder):
    """Nombre de sacs PHYSIQUES du set : le plus grand numero de fichier (« 9 » et
    « 9 - 1 » sont le meme sac 9). 0 s'il n'y en a aucun."""
    from .plan import discover_bags, bag_sort_key
    total = 0
    for label, _p, _i in discover_bags(folder):
        kind, main, _sub, _ = bag_sort_key(label)
        if kind == 0:
            total = max(total, main)
    return total


def set_fully_sorted(folder):
    """Ce set est-il TERMINÉ : cataloguage fini, et tous ses sacs cochés ?

    Les deux conditions comptent. Un set à moitié catalogué dont les sacs du jour sont
    cochés n'est pas fini — d'autres sacs vont naître (voir `sortable_while_cataloguing`)."""
    from .plan import set_number
    if not _catalogue_done(folder):
        return False
    total = bag_total(folder)
    if not total:
        return False
    from . import remote as _remote
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    done = (state.get(set_number(folder)) or {}).get("bags_done") or []
    return len([b for b in done if 1 <= b <= total]) >= total


def rest_pending(set_no):
    """(nom de l'envoi, [autres sets pas encore finis]) pour un set TRIÉ dont l'envoi ne
    l'est pas ; None sinon.

    C'est l'état « je n'ai plus rien à faire sur ce set, mais il ne part pas encore » : le
    maître couvre tout l'envoi, donc tant qu'un seul de ses sets est en cours, rien ne
    s'expédie. Sans ça la liste affichait « Trié ✓ », qui se lit comme « prêt à partir »."""
    name = owning_batch(set_no)
    if not name:
        return None
    rest = []
    for s in get(name).get("sets", []):
        if str(s) == str(set_no):
            continue
        f = folder_of(s)
        if f is None or not set_fully_sorted(f):
            rest.append(str(s))
    return (name, rest) if rest else None


def sortable_while_cataloguing(folder):
    """Ce set peut-il être TRIÉ alors que son cataloguage n'est pas fini ?

    Oui quand il appartient à un envoi FERMÉ et qu'au moins un de ses sacs porte déjà des
    remarques. Les deux conditions disent la même chose vue de deux côtés : l'envoi est
    fermé, donc la numérotation des Sacs ne bougera plus (c'est la règle de
    `settle_phases`) ; des remarques sont écrites, donc il y a de quoi trier.

    Pourquoi l'autoriser : les Sacs d'un batch restent OUVERTS sur la table pendant tout
    l'envoi. Attendre la fin du cataloguage d'un set pour vider ses premiers sacs dedans
    n'apporte rien — la place est là, les numéros sont fixés, et le reste du cataloguage ne
    peut que rajouter des pièces dans ces mêmes Sacs. Hors batch, la question ne se pose
    pas : un set seul se numérote à partir de son contenu, qui n'est pas encore connu."""
    from .plan import set_number
    name = owning_batch(set_number(folder))
    if not name:
        return False
    try:
        if not get(name).get("closed"):
            return False
    except KeyError:
        return False
    try:
        from . import remote as _remote
        return _remote.coverage(folder)[1] > 0
    except Exception:
        return False


# --- résoudre les sets en dossiers --------------------------------------------

def folder_of(set_no):
    """Le dossier de ce set, à la racine ou dans un batch. None s'il n'existe plus."""
    for f in iter_set_folders():
        if set_number(f) == str(set_no):
            return f
    return None


def folders(name, partial=False):
    """Les dossiers du batch, dans l'ordre déclaré. Lève si l'un manque : planifier un batch
    amputé donnerait une numérotation de Sacs qui ne vaut que pour cette exécution.

    `partial=True` rend ce qui EST là sans lever — réservé au cas où un PLAN PRÉVISIONNEL
    tient déjà la numérotation (voir plan_inputs) : c'est lui, et non les dossiers présents,
    qui décide alors des Sacs. Sans plan prévisionnel, l'exception reste la bonne réponse."""
    b = get(name)
    out, missing = [], []
    for s in b["sets"]:
        f = folder_of(s)
        (out if f else missing).append(f or s)
    if missing and not partial:
        raise RuntimeError("dossier introuvable pour : %s" % ", ".join(missing))
    return out


# --- l'état du batch ----------------------------------------------------------

def _pieces(folder):
    """Pièces physiques du set (sacs + inventaire), à l'échelle ×N."""
    from .plan import set_multiplier_of
    m = set_multiplier_of(folder)[0]
    tot = 0
    for name in os.listdir(folder):
        if not name.lower().endswith(".bsx"):
            continue
        p = os.path.join(folder, name)
        with open(p, "r", encoding="utf-8") as f:
            scale = m if bsx.multiplication_state(f.read(), m) == "base" else 1
        tot += bsx.total_qty(p) * scale
    return tot


def _expected_pieces(set_no):
    """Pièces qu'un set INSCRIT mais pas encore arrivé apportera : son compte au catalogue
    (extras inclus — la boîte les contient) × les exemplaires achetés, lus dans la table
    `inventory`, donc dans le Journal.

    Sans ça, un envoi ne se sait complet qu'une fois les colis reçus ET catalogués, alors
    que c'est décidé le jour de l'achat. Rend 0 dès qu'il manque une des deux moitiés — pas
    d'achat enregistré, ou set inconnu du catalogue : on ne devine pas."""
    try:
        from . import forecast, catalogdb
        copies = forecast.copies_of(set_no)
        if not copies:
            return 0
        parts = catalogdb.shared().inventory(str(set_no).split("-")[0], include_extras=True)
        return sum(p.qty for p in parts) * int(copies)
    except Exception:
        return 0


def status(name):
    """De quoi décider si le batch est prêt à fermer, et ce qu'il contient.

    `pieces` est le total de l'envoi TEL QU'IL SERA : ce qui est physiquement là plus ce que
    les sets déjà achetés apporteront. C'est cette somme qui décide de `full`, parce que la
    question « l'envoi est-il complet ? » se tranche à l'achat, pas à la réception — sinon un
    batch acheté au complet continuerait à réclamer des sets pendant des semaines.
    `pieces_here` / `pieces_expected` gardent le détail, pour que rien ne soit masqué."""
    b = get(name)
    rows, here_total, exp_total = [], 0, 0
    for s in b["sets"]:
        f = folder_of(s)
        bags = discover_bags(f) if f else []
        # Un DOSSIER n'est pas une livraison. `purchases.sync` en crée un dès l'achat, avec un
        # fichier d'inventaire tiré du catalogue — le set peut très bien être encore chez le
        # transporteur. Ce qui prouve qu'il est là, c'est qu'on ait commencé à le cataloguer,
        # donc des SACS NUMÉROTÉS. Sans eux, ses pièces restent attendues.
        if bags:
            pc, exp = _pieces(f), 0
        elif f:
            pc, exp = 0, _pieces(f)      # dossier seul : le compte vient de son inventaire
        else:
            pc, exp = 0, _expected_pieces(s)
        here_total += pc
        exp_total += exp
        here = bool(f and os.path.abspath(os.path.dirname(f))
                    == os.path.abspath(batch_dir(name)))
        rows.append({"set": s, "folder": f, "pieces": pc, "expected": exp,
                     "bags": len(bags),
                     "inventory": bool(f and find_inventory(f)),
                     # False = son dossier n'a pas encore pu être déplacé dans l'envoi
                     # (verrou Drive / explorateur ouvert) : `settle` réessaie à chaque passe
                     "in_folder": here,
                     })
    total = here_total + exp_total
    return {"name": name, "closed": b.get("closed", False), "created": b.get("created"),
            "sets": rows, "pieces": total, "pieces_here": here_total,
            "pieces_expected": exp_total, "target": TARGET_PIECES,
            "full": total >= TARGET_PIECES,
            "missing": [r["set"] for r in rows if not r["folder"]],
            "pending": [r["set"] for r in rows if r["expected"]]}


# --- le plan prévisionnel -----------------------------------------------------

def forecast_path(name):
    """Le plan prévisionnel vit DANS le dossier du batch : il décrit cet envoi-là et doit
    partir avec lui."""
    from .forecast import FORECAST_FILE
    return os.path.join(batch_dir(name, create_it=True), FORECAST_FILE)


def forecast(name, copies=None, save=True, log=print):
    """Calcule (et range) la Phase A du batch depuis le CATALOGUE, sans attendre que ses sets
    soient arrivés ni catalogués — voir sortpack/forecast.py pour le pourquoi et les limites.

    `copies` : {numéro: exemplaires} pour forcer un ×N ; sinon il sort de la table
    `inventory`, donc du Journal. Une fois rangé, `plan()` et `apply()` s'y accrochent
    automatiquement : le set qu'on a déjà se trie aux numéros de Sacs de l'envoi complet."""
    from . import forecast as _fc
    b = get(name)
    copies = copies or {}
    sets = [(s, copies.get(str(s))) for s in b["sets"]]
    started = sorting_started(name)
    if started and log:
        log("⚠ le tri est DÉJÀ engagé sur %s : ce nouveau plan leur donnera d'autres numéros "
            "de Sacs que ceux écrits dans leurs fichiers." % ", ".join(started))
        log("  (un Apply ré-épingle ce qui est écrit, mais les deux numérotations ne se "
            "rejoindront pas — ne recalcule que si rien n'est en sac.)")
    res = _fc.compute(sets)
    if log:
        _fc.report(res, log=log)
    if save:
        path = _fc.save(res, forecast_path(name), name=name)
        if log:
            log("rangé dans %s" % path)
    return res


def load_forecast(name):
    """Le plan prévisionnel du batch, ou None. Rend aussi les ÉCARTS de composition depuis
    qu'il a été figé — un set ajouté ou un ×N changé rend faux tout ce qui est déjà trié,
    donc personne ne doit s'en servir sans les avoir vus."""
    from . import forecast as _fc
    data = _fc.load(forecast_path(name))
    if data is None:
        return None
    data["drift"] = _fc.verify_composition(data, get(name)["sets"])
    return data


def sorting_started(name):
    """Les sets du batch dont le tri est PHYSIQUEMENT engagé — un sac coché, la phase passée
    à « Trier », ou l'ancien tampon des restants.

    C'est `remote.sorting_started` qui en décide, exactement comme pour l'auto-apply : la
    question « peut-on encore renuméroter les Sacs de ce set ? » ne doit avoir qu'une seule
    réponse dans le projet. Surtout pas « ses fichiers portent des remarques » — l'auto-apply
    en écrit sur tout set catalogué, donc ça vaudrait toujours oui."""
    from . import remote as _remote
    out = []
    for f in folders(name, partial=True):
        try:
            if _remote.sorting_started(f):
                out.append(set_number(f))
        except Exception:
            continue
    return out


def pending_keys(name):
    """Les (pièce, couleur) que l'envoi apportera ENCORE — ce qui n'est pas dans ses sacs.

    C'est ce qui manquait à la Phase B. Phase A planifie sur tout l'envoi (le plan
    prévisionnel le lui donne), mais Phase B ne voyait que les sacs présents : elle vidait sa
    boîte C au dernier sac du set en main alors que la même pièce, même couleur, arrive dans
    un set qui n'est pas encore là. Mesuré sur 11503 + Jaguar : 8 lots partaient trop tôt.

    Trois cas, du plus flou au plus précis :

    * **pas de dossier, ou aucun sac** — on prend tout l'inventaire du set au catalogue : on
      ne sait rien de plus ;
    * **cataloguage EN COURS** — les sacs faits sont déjà dans le plan ; ce qui reste à venir
      est exactement le fichier d'inventaire de restes, qu'on lit tel quel. C'est le cas que
      l'ancienne version ratait : dès qu'un set avait UN sac elle le considérait complet, et
      annoter un envoi en cours de cataloguage aurait refermé les boîtes C trop tôt ;
    * **cataloguage fini** — rien n'arrive plus, on saute.

    On ne sait pas DANS QUEL sac la couleur arrivera — ça, seul le cataloguage le dira — mais
    on sait qu'elle arrivera, et c'est la seule question que Phase B pose."""
    from .plan import find_inventory, part_key
    out = set()
    cat = None
    for s in get(name).get("sets", []):
        f = folder_of(s)
        if f and discover_bags(f):
            if _catalogue_done(f):
                continue                  # catalogué : ses sacs sont déjà dans le plan
            inv = find_inventory(f)       # en cours : ce qui reste à cataloguer est ICI
            if inv:
                try:
                    for it in bsx.read_items(inv):
                        out.add(part_key(it))
                except Exception:
                    pass
                continue
        if cat is None:
            try:
                from . import catalogdb
                cat = catalogdb.shared()
            except Exception:
                return out
        try:
            for part in cat.inventory(str(s).split("-")[0], include_extras=True):
                out.add((str(part.item_id), str(part.color_id), "N"))
        except Exception:
            continue                      # set inconnu du catalogue : on ne devine pas
    return out


def _autoforecast(name, log=None):
    """Calcule et range le plan prévisionnel quand le batch en a besoin et n'en a pas.

    Le besoin : un set inscrit **sans aucune liste**. Il n'apporte rien à la Phase A, qui
    numéroterait alors les Sacs pour une fraction de l'envoi — 8 Sacs au lieu de 25 dans le
    cas qui a fait trouver ça. Exiger un clic avant le premier Apply était un piège : on
    appliquait, on obtenait une numérotation trop courte, et rien ne le disait.

    Le fichier reste rangé, parce que son rôle n'est pas d'identifier le batch mais de
    **figer** la numérotation : une fois écrit, elle ne bougera plus sous le trieur, et
    `verify_composition` signalera tout écart de composition.

    On REFUSE plutôt que de deviner quand un set à venir n'a aucun achat enregistré : son ×N
    vaudrait 0, et un plan calculé sur un set absent est pire que pas de plan."""
    try:
        missing = [s for s in get(name).get("sets", [])
                   if not (folder_of(s) and discover_bags(folder_of(s)))]
    except Exception:
        return None
    if not missing:
        return None                      # tout est catalogué : la Phase A se suffit
    from . import forecast as _fc
    sans_achat = [s for s in missing if not _fc.copies_of(s)]
    if sans_achat:
        raise RuntimeError(
            "le batch « %s » ne peut pas être planifié : %s n'%s aucune liste ni achat "
            "enregistré.\nLogue l'achat au Journal (le ×N en dépend), ou retire-le de "
            "l'envoi." % (name, ", ".join(sans_achat),
                          "a" if len(sans_achat) == 1 else "ont"))
    if log:
        log("Plan prévisionnel calculé automatiquement : %s n'%s pas encore de liste."
            % (", ".join(missing), "a" if len(missing) == 1 else "ont"))
    res = forecast(name, log=log or (lambda _m: None))
    data = load_forecast(name)
    if data is not None and log:
        log("  (figé dans %s — il ne bougera plus sans un recalcul explicite)"
            % os.path.basename(forecast_path(name)))
    return data


def _ticked(set_no):
    """Ce set a-t-il quelque chose de PHYSIQUEMENT en sac ? (liste cochée, restants placés)

    C'est la seule preuve qui interdise de renuméroter. La PHASE, elle, ne prouve rien par
    elle-même : on la pose à la main, parfois des jours avant de commencer."""
    from . import remote as _remote
    f = folder_of(set_no)
    if f and _remote.restants_done(f):
        return True
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    return bool((state.get(str(set_no)) or {}).get("bags_done"))


def _layout_disagrees(name, layout):
    """Les Sacs ÉCRITS dans les listes contredisent-ils le plan prévisionnel ?

    Vrai juste après qu'un plan ait été figé alors que des remarques existaient déjà — le cas
    qui laissait le Flower Wall sur 8 Sacs au lieu de 25."""
    if not layout:
        return False
    from .plan import existing_labels
    for f in folders(name, partial=True):
        try:
            written = existing_labels(discover_bags(f))["sac"]
        except Exception:
            continue
        for k, n in written.items():
            if layout.get(k) not in (None, n):
                return True
    return False


def settle_phases(log=None, _applied=None):
    """Aligne la phase des sets de batch sur l'état de leur envoi — et applique quand il faut.

    Trois règles, qui tiennent dans une phrase chacune :

    * **un envoi pas encore fermé ne se trie pas.** Tant qu'un set peut entrer, la
      numérotation des Sacs n'est pas figée. Un set poussé à « Trier » redescend donc en
      « En attente de fermeture du batch », et rien n'est écrit ;
    * **à la fermeture, on applique et on passe à Trier.** Les sets en attente reçoivent les
      remarques de tout l'envoi, puis leur phase bascule. C'est l'application qui fait entrer
      dans le tri, pas l'inverse ;
    * **un plan figé après coup se rattrape.** Si les Sacs écrits contredisent le plan
      prévisionnel et que RIEN n'est physiquement en sac, on ré-applique sans épingler.

    Rien n'est jamais renuméroté dès qu'une liste est cochée : `_ticked` est le seul veto.
    Appelée par le rafraîchissement du GUI et par le poll du PC, donc une phase posée depuis
    le téléphone est rattrapée au passage suivant.
    """
    from . import remote as _remote
    log = log or (lambda _m: None)
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    changed, applied = False, list(_applied or [])

    for name in sorted(all_batches()):
        b = get(name)
        sets = [str(s) for s in b.get("sets", [])]
        closed = bool(b.get("closed"))

        if not closed:
            for s in sets:
                if (state.get(s) or {}).get("status") == config.STATUS_TRIER and not _ticked(s):
                    state.setdefault(s, {})["status"] = config.STATUS_ATTENTE
                    changed = True
                    log("%s : envoi « %s » pas encore fermé → %s"
                        % (s, name, config.STATUS_ATTENTE))
            continue

        waiting = [s for s in sets
                   if (state.get(s) or {}).get("status") == config.STATUS_ATTENTE]
        heal = []
        if not waiting:
            try:
                _fs, layout = plan_inputs(name)
                if _layout_disagrees(name, layout):
                    heal = [s for s in sets
                            if (state.get(s) or {}).get("status") == config.STATUS_TRIER
                            and not _ticked(s)]
            except Exception as e:
                log("%s : plan indisponible (%s)" % (name, str(e)[:60]))
        todo = waiting or heal
        if not todo or any(_ticked(s) for s in todo):
            continue
        if name in applied:
            continue
        try:
            if heal:
                log("Envoi « %s » : les Sacs écrits ne suivent pas le plan prévisionnel et "
                    "rien n'est en sac → ré-application sans épinglage." % name)
            else:
                log("Envoi « %s » fermé → application des remarques puis passage à « %s »."
                    % (name, config.STATUS_TRIER))
            apply(name, log=log)
            applied.append(name)
            record_applied(name)         # l'auto-apply d'envoi ne le refera pas derrière
        except Exception as e:
            log("⚠ envoi « %s » non appliqué : %s" % (name, str(e)[:80]))
            continue
        for s in todo:
            state.setdefault(s, {})["status"] = config.STATUS_TRIER
            changed = True

    if changed:
        _remote._write_json(config.SET_STATUS_FILE, state)
    return applied


def request_trier(set_no, log=None):
    """« Je commence à trier ce set » — le geste du clic droit et du téléphone.

    Rend la phase RÉELLEMENT posée : « Trier » si l'envoi est fermé (et les remarques viennent
    d'être écrites), « En attente de fermeture du batch » sinon. Hors batch, rien ne change :
    le set passe à Trier comme avant."""
    from . import remote as _remote
    log = log or (lambda _m: None)
    s = str(set_no)
    name = owning_batch(s)
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    if not name:
        state.setdefault(s, {})["status"] = config.STATUS_TRIER
        _remote._write_json(config.SET_STATUS_FILE, state)
        return config.STATUS_TRIER
    phase = (config.STATUS_TRIER if get(name).get("closed")
             else config.STATUS_ATTENTE)
    state.setdefault(s, {})["status"] = phase
    _remote._write_json(config.SET_STATUS_FILE, state)
    if phase == config.STATUS_ATTENTE:
        log("%s appartient à l'envoi « %s », qui n'est pas fermé : %s."
            % (s, name, config.STATUS_ATTENTE))
    else:
        settle_phases(log=log)
    return phase


def plan_inputs(name, log=None):
    """(dossiers, layout) — comment planifier ce batch dans l'état où il est.

    Sans plan prévisionnel : tous les dossiers doivent être là (folders lève sinon).
    Avec : on planifie ce qui est arrivé, épinglé sur la numérotation prévue.

    Un batch dont un set n'a AUCUNE LISTE n'est pas planifiable tel quel : ce set n'apporte
    rien à la Phase A, qui numérote alors les Sacs pour une fraction de l'envoi. On calcule
    donc le plan prévisionnel à la volée — voir `_autoforecast`.

    Un plan PÉRIMÉ n'est jamais utilisé en douce. Il a été calculé pour une composition qui
    n'est plus celle du batch, donc ses numéros de Sacs ne valent plus rien : s'en servir
    quand même écrirait des remarques fausses sur des sets qu'on est peut-être en train de
    trier. On s'arrête et on dit quoi faire — c'est une décision, pas un détail."""
    fc = load_forecast(name)
    if fc is None:
        fc = _autoforecast(name, log=log)
    if fc is None:
        return folders(name), None
    if fc.get("drift"):
        started = sorting_started(name)
        lines = ["le plan prévisionnel du batch « %s » ne correspond plus à sa "
                 "composition :" % name]
        lines += ["  - %s" % d for d in fc["drift"]]
        lines.append("Recalcule-le (« Plan prévisionnel » / "
                     "python -m sortpack.batch forecast %s)." % name)
        if started:
            lines.append("ATTENTION : le tri est déjà engagé sur %s — recalculer changera "
                         "leurs Sacs. Ne le fais que si rien n'est encore en sac."
                         % ", ".join(started))
        else:
            lines.append("Rien n'est encore trié : recalculer est sans risque.")
        raise RuntimeError("\n".join(lines))
    return folders(name, partial=True), fc["layout"]


# --- planifier / appliquer / finaliser ----------------------------------------

def plan(name, wdb=None, pin_sacs=False):
    """La Phase A sur l'union des sets du batch. Une seule numérotation de Sacs.

    `open_sacs=True` : les Sacs du batch restent ouverts sur la table, donc pas de boîte D
    (voir le module). Réglable par config.BATCH_OPEN_SACS si jamais on trie autrement."""
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()
    settle()                              # un set fraîchement catalogué rejoint le batch
    fs, layout = plan_inputs(name)
    return build_plan(fs, wdb, pin_sacs=pin_sacs, pinned_layout=layout,
                      pending_keys=pending_keys(name),
                      open_sacs=getattr(config, "BATCH_OPEN_SACS", True))


def consolidation_pass(p):
    """Ce qu'il reste à faire quand tout est trié : par Sac, les sous-sacs à confectionner.

    Un sous-sac par moule multi-couleurs — ce que le client exige. La passe est LOCALE : les
    couleurs d'un moule sont déjà dans le même Sac (Phase A les y a mises), donc on n'ouvre
    jamais qu'un Sac à la fois. Renvoie [{sac, lots, sous_sacs: [{mould, colors}]}]."""
    per = {}
    for k, s in p["sac_of"].items():
        per.setdefault(s, []).append(k)
    out = []
    for s in sorted(per):
        moulds = {}
        for k in per[s]:
            moulds.setdefault(mould_key(k), []).append(k)
        subs = [{"mould": m[0],
                 "colors": sorted(p["meta"][k]["color_name"] or "?" for k in ks)}
                for m, ks in sorted(moulds.items()) if len(ks) > 1]
        out.append({"sac": s, "lots": len(per[s]), "sous_sacs": subs})
    return out


# Étiquette des lignes du fichier de consolidation. Le tri texte sur Remarks regroupe
# d'abord par Sac, puis par moule — donc les couleurs d'un même sous-sac se suivent.
CONSO_GROUP = "Sac {sac} · {mould} ({colors})"


def build_consolidation_bsx(name, out_dir=None, wdb=None, log=print):
    """Le fichier de la passe de consolidation, à ouvrir dans BrickStore.

    Il ne contient QUE les lots à regrouper : les moules présents en plusieurs couleurs dans
    un même Sac. Un moule d'une seule couleur n'a rien à consolider — il reste où il est, et
    l'écrire ici ne ferait que noyer le travail réel.

    Une ligne par lot, avec en Remarks le Sac, le sous-sac à confectionner et les couleurs
    qui doivent s'y retrouver :

        Sac 03 · 4042 (Bright Green, Sand Green)
        Sac 03 · 4042 (Bright Green, Sand Green)

    Tu tries sur la colonne Remarks et tu descends Sac par Sac : les couleurs d'un même
    sous-sac se suivent, puisque l'étiquette est identique sur chacune. C'est la même
    mécanique que les remarques de tri, avec la même mise en page de colonnes (le GuiState
    des listes de sacs est injecté), donc rien de neuf à apprendre.

    Les lots communs à plusieurs sets du batch sont fusionnés en une ligne — dans le Sac ils
    ne forment qu'un seul lot."""
    from .restants import _emit_item, _document
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()
    out_dir = out_dir or batch_dir(name, create_it=True)
    p = plan(name, wdb=wdb, pin_sacs=True)

    # un bloc <Item> représentatif par lot, quantités cumulées entre les sets
    blocks, qty, currency = {}, {}, "CAD"
    for _label, path, items in p["bags"]:
        raw_blocks, raw = bsx.read_item_blocks(path)
        m = re.search(r'<Inventory[^>]*\bCurrency="([^"]+)"', raw)
        if m:
            currency = m.group(1)
        for it in items:
            k = (it["item_id"], it["color_id"], it["condition"])
            blocks.setdefault(k, raw_blocks[it["row"]])
            qty[k] = qty.get(k, 0) + it["qty"]

    # par Sac, puis par moule : les groupes à faire
    per_sac = {}
    for k, s in p["sac_of"].items():
        per_sac.setdefault(s, {}).setdefault(mould_key(k), []).append(k)

    texts, groups, sacs = [], 0, set()
    for s in sorted(per_sac):
        for m, keys in sorted(per_sac[s].items()):
            if len(keys) < 2:
                continue                 # une seule couleur : rien à regrouper
            groups += 1
            sacs.add(s)
            colors = ", ".join(sorted(p["meta"][k]["color_name"] or "?" for k in keys))
            remark = CONSO_GROUP.format(sac=f"{int(s):0{config.LABEL_PAD}d}",
                                        mould=m[0], colors=colors)
            for k in sorted(keys, key=lambda kk: p["meta"][kk]["color_name"] or ""):
                if k in blocks:
                    texts.append(_emit_item(blocks[k], qty.get(k, 0), remark))

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{name} - consolidation.bsx")
    # Même mise en page que les listes de sacs : le fichier s'ouvre avec les bonnes colonnes,
    # déjà trié sur Remarks — donc les couleurs d'un sous-sac arrivent l'une sous l'autre.
    doc = bsx.set_gui_state(_document(currency, texts))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(doc)
    log(f"Consolidation : {groups} sous-sacs sur {len(sacs)} Sacs, "
        f"{len(texts)} lots → {path}")
    return {"path": path, "groups": groups, "sacs": len(sacs), "lots": len(texts)}


def _report_layout_conflict(p, layout, started, log):
    """Épingler garde ce qui est ÉCRIT ; le plan prévisionnel dit parfois autre chose. Le cas
    arrive une fois : un set remarqué avant que le plan existe, et dont la phase est déjà
    « Trier » alors que rien n'est encore physiquement en sac. Personne ne doit avoir à le
    deviner — on dit combien de lots gardent l'ancien numéro, et quoi faire."""
    if not (layout and started):
        return
    clash = [k for k, s in p["sac_of"].items() if layout.get(k) not in (None, s)]
    if not clash:
        return
    log("⚠ %d lot(s) gardent le Sac écrit dans leurs fichiers plutôt que celui du plan "
        "prévisionnel — le tri est marqué en cours sur %s, donc rien n'a été renuméroté."
        % (len(clash), ", ".join(started)))
    log("  Si rien n'est PHYSIQUEMENT en sac : remets la phase de ces sets avant « Trier » "
        "(clic droit dans la liste) et ré-applique — le plan prévisionnel prendra alors la "
        "main. Sinon, garde les Sacs écrits : ils correspondent à ce qui est dans les bacs.")


def apply(name, wdb=None, backup=None, log=print):
    """Écrit les remarques du batch dans les fichiers de CHAQUE set.

    Chaque set est vérifié d'abord (comme un Apply normal), puis multiplié par SON propre ×N
    — deux sets du même batch viennent d'achats différents. Le plan, lui, est commun.

    **Dès qu'un set du batch est en cours de tri, le plan est ÉPINGLÉ** (`pin_sacs`). C'est le
    cas normal avec un plan prévisionnel : on trie le premier set, les suivants arrivent, on
    ré-applique. Sans épingler, Phase B repartirait de zéro — et une couleur que le premier
    set a déjà envoyée dans son Sac redeviendrait « à garder en boîte C », parce que le
    nouveau set en apporte encore. La remarque contredirait alors des pièces déjà en sac.
    Épinglé, ce qui est écrit fait foi (`terminal`) et les nouvelles pièces rejoignent le Sac
    où les premières sont déjà — ce que `existing_labels` sait faire depuis toujours."""
    from . import verify
    from .apply import backup_files
    if wdb is None:
        from .weightdb import WeightDB
        wdb = WeightDB()
    if backup is None:
        backup = config.ENABLE_BACKUP
    settle(log=log)
    fs, layout = plan_inputs(name, log=log)
    # On épingle sur la PREUVE PHYSIQUE (liste cochée, restants placés), pas sur la phase :
    # depuis settle_phases, c'est l'application qui fait passer un set à « Trier », donc se
    # fier à la phase reviendrait à refuser d'écrire ce qu'on est justement en train d'écrire.
    started = [s for s in get(name).get("sets", []) if _ticked(s)]
    if started:
        log("déjà en sac sur %s — plan épinglé : rien ne bouge."
            % ", ".join(str(s) for s in started))

    if getattr(config, "VERIFY_ON_APPLY", True):
        for f in fs:
            verify.before_apply(f, log=log, backup=backup)

    p = build_plan(fs, wdb, pin_sacs=bool(started), pinned_layout=layout,
                   pending_keys=pending_keys(name),
                   open_sacs=getattr(config, "BATCH_OPEN_SACS", True))
    _report_layout_conflict(p, layout, started, log)
    # **Rien n'est multiplié tant qu'un set n'est pas fini d'être catalogué.** Les
    # remarques, elles, s'écrivent tout de suite : le plan calcule ses Sacs sur les
    # quantités PHYSIQUES quoi qu'il arrive (build_plan compense l'échelle fichier par
    # fichier, `file_mult`), donc un sac annoté à la quantité de base porte exactement les
    # mêmes numéros de Sacs qu'après multiplication. Reporter le ×N évite le seul état
    # vraiment dangereux : un fichier MÉLANGÉ. Le poll tombe forcément un jour au milieu
    # d'un sac qu'on est en train de saisir — s'il le multipliait, les lots ajoutés
    # ensuite resteraient à ×1 dans un fichier tamponné ×N, et plus personne ne les
    # remonterait. À l'échelle de base, un sac à moitié saisi n'est qu'un sac à moitié
    # saisi : la passe suivante le reprend tel quel.
    done = {f: _catalogue_done(f) for f in fs}
    paths = [path for _l, path, _i in p["bags"] if p["remarks"].get(path)]
    backup_dir = None
    if backup and paths:
        for f in fs:
            own = [x for x in paths if os.path.dirname(x) == f]
            if own:
                backup_dir = backup_files(f, own)

    total = 0
    for path in paths:
        mult = p["path_mult"].get(path, 1)
        if not done.get(os.path.dirname(path), True):
            mult = 1                     # cataloguage en cours : remarques seules
        n, _eff, _st = bsx.write_plan(path, p["remarks"][path], multiplier=mult)
        total += n

    # Les sacs sont maintenant à l'échelle physique (×N) ; l'inventaire de restes suit —
    # sauf pendant le cataloguage, où il sert encore de plan de travail (la règle est dans
    # verify.normalize_inventory, qui la tient pour tout le projet).
    from .plan import set_multiplier_of
    for f in fs:
        verify.normalize_inventory(f, set_multiplier_of(f)[0], log=log)
        if not done.get(f, True):
            log("• %s : cataloguage en cours — annoté à la quantité d'un exemplaire "
                "(sacs et inventaire passeront à l'échelle d'un coup, à la fin)."
                % os.path.basename(f))

    from . import restants
    for f in fs:
        restants.write_bag_help(f, wdb=wdb)

    log(f"✔ Batch « {name} » : {total} lots annotés dans {len(fs)} set(s), "
        f"{p['stats']['sacs']} Sacs.")
    return {"batch": name, "lots": total, "sacs": p["stats"]["sacs"],
            "folders": fs, "plan": p, "backup_dir": backup_dir}


# --- auto-apply de l'envoi (pas de bouton) ------------------------------------
#
# Le pendant batch de `remote.auto_apply_ready`. Celui-ci ne touche jamais un set de batch
# (il le planifierait seul, avec une numérotation de Sacs à lui) — alors c'est ici que les
# remarques d'un envoi se réécrivent toutes seules à mesure que son cataloguage avance.
#
# La décision lit les FICHIERS, comme par set : une empreinte « lots annotés / lots » sur
# tous les sacs de l'envoi. Elle change quand un sac apparaît (cataloguage qui avance) ou
# quand des remarques disparaissent (BrickStore qui réenregistre un vieil onglet) — les deux
# cas où il faut réécrire. Elle ne change pas quand rien n'a bougé : aucun catalogue chargé,
# aucune sauvegarde, aucune réécriture.

BATCH_APPLY_FILE = os.path.join(config.CATALOGUAGE_ROOT, "batch_apply.json")


def _coverage_fingerprint(name):
    """L'empreinte de l'envoi : « 11381:12/12:C|11503:97/97:F » — par set, les lots annotés
    sur les lots de ses sacs, et F/C selon que son cataloguage est Fini ou en Cours. Texte
    seul, donc on peut la demander à chaque passage de la boucle de fond.

    L'état du cataloguage en fait partie parce que c'est lui qui décide de la
    MULTIPLICATION : tant qu'il est en cours on annote à la quantité de base, et le passage
    à « fini » est ce qui déclenche le ×N sur tous les sacs. Sans lui, un cataloguage
    terminé en VIDANT les derniers lots de l'inventaire (plutôt qu'en les versant dans un
    sac) ne changerait aucune couverture : l'envoi resterait à la quantité de base pour
    toujours, et `finalize` — qui replie les fichiers tels quels — expédierait un maître à
    1/N."""
    from . import remote as _remote
    parts = []
    for f in sorted(folders(name, partial=True)):
        try:
            lots, stamped = _remote.coverage(f)
        except Exception:
            lots, stamped = -1, -1
        parts.append(f"{set_number(f)}:{stamped}/{lots}:{'F' if _catalogue_done(f) else 'C'}")
    return "|".join(parts)


def _quiet_since(fs):
    """Depuis combien de secondes plus aucun .bsx de ces dossiers n'a bougé ? (None si
    on ne peut pas lire les dates — on ne fait alors attendre personne.)"""
    import time
    last = None
    for f in fs:
        try:
            names = os.listdir(f)
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(".bsx"):
                continue
            try:
                t = os.path.getmtime(os.path.join(f, n))
            except OSError:
                continue
            last = t if last is None else max(last, t)
    return None if last is None else max(0.0, time.time() - last)


def auto_apply_action(name, tracked=None):
    """Ce que la passe de fond doit faire de cet envoi :
         "apply"  - (ré)écrire ses remarques maintenant
         "record" - déjà entièrement annoté, juste s'en souvenir (aucun travail)
         "wait"   - quelque chose vient de bouger : on laisse finir la saisie
         "skip"   - rien à faire

    Un envoi PAS FERMÉ est toujours « skip » : sa composition peut encore changer, donc sa
    numérotation de Sacs n'est pas figée. C'est `settle_phases` qui écrit à la fermeture.
    """
    tracked = _read_json_safe(BATCH_APPLY_FILE) if tracked is None else tracked
    try:
        b = get(name)
    except KeyError:
        return "skip"
    if not b.get("closed") or is_finished(b):
        return "skip"
    fs = [f for f in folders(name, partial=True) if discover_bags(f)]
    if not fs:
        return "skip"                     # aucun sac dans tout l'envoi : rien à annoter
    entry = tracked.get(name)
    if isinstance(entry, dict) and entry.get("fp") == _coverage_fingerprint(name):
        return "skip"                     # inchangé depuis qu'on a écrit
    from . import remote as _remote
    # « record » = tout est annoté ET à l'échelle : il n'y a vraiment rien à écrire. Un set
    # fini d'être catalogué mais encore à la quantité de base doit passer par « apply »,
    # c'est cette passe-là qui le multiplie.
    if (all(_remote.already_remarked(f) for f in fs)
            and not any(_needs_scaling(f) for f in fs)):
        return "record"
    # Quelque chose a bougé il y a quelques secondes : c'est très probablement un sac en
    # cours de saisie. On repasse au tour suivant — voir BATCH_APPLY_QUIET_SECONDS.
    quiet = getattr(config, "BATCH_APPLY_QUIET_SECONDS", 120)
    since = _quiet_since(fs)
    if quiet and since is not None and since < quiet:
        return "wait"
    return "apply"


def _needs_scaling(folder):
    """Ce set est-il fini d'être catalogué mais encore à la quantité d'un exemplaire ?
    C'est le travail que seule une vraie passe d'Apply peut faire."""
    from .plan import set_multiplier_of, discover_bags
    if not _catalogue_done(folder):
        return False
    try:
        m = set_multiplier_of(folder)[0]
    except Exception:
        return False
    if not m or m <= 1:
        return False
    for _label, path, _items in discover_bags(folder):
        with open(path, "r", encoding="utf-8") as f:
            if bsx.multiplication_state(f.read(), m) == "base":
                return True
    return False


def record_applied(name):
    """Fige l'empreinte courante de l'envoi : « ce qui est sur le disque, c'est ce que j'ai
    écrit ». Appelé après tout Apply d'envoi, d'où qu'il vienne, pour que la passe de fond
    ne réécrive pas immédiatement derrière."""
    from . import remote as _remote
    try:
        tracked = _read_json_safe(BATCH_APPLY_FILE)
        tracked[name] = {"at": _remote._now(), "fp": _coverage_fingerprint(name)}
        _remote._write_json(BATCH_APPLY_FILE, tracked)
    except Exception:
        pass


def _read_json_safe(path):
    from . import remote as _remote
    d = _remote._read_json(path, {})
    return d if isinstance(d, dict) else {}


def has_auto_apply_candidate():
    """Un envoi attend-il vraiment d'être réécrit ? Contrôle bon marché (aucun poids chargé)
    qui dit à la boucle de fond s'il faut sortir le catalogue BrickStore."""
    if not getattr(config, "BATCH_AUTO_APPLY", True):
        return False
    tracked = _read_json_safe(BATCH_APPLY_FILE)
    for name in all_batches():
        try:
            if auto_apply_action(name, tracked) == "apply":
                return True
        except Exception:
            continue
    return False


def auto_apply_all(wdb=None, backup=None, log=print):
    """Réécrit les remarques des envois fermés dont les sacs ont bougé. Renvoie le nombre
    d'envois appliqués. Un envoi que `settle_phases` vient d'écrire porte déjà l'empreinte
    courante (`record_applied`), donc il n'est pas réécrit derrière."""
    if not getattr(config, "BATCH_AUTO_APPLY", True):
        return 0
    tracked = _read_json_safe(BATCH_APPLY_FILE)
    from . import remote as _remote
    changed = done = 0
    for name in sorted(all_batches()):
        try:
            action = auto_apply_action(name, tracked)
        except Exception as e:
            log(f"⚠ envoi « {name} » : état illisible ({str(e)[:70]})")
            continue
        if action == "skip":
            continue
        if action == "wait":
            continue                     # saisie en cours : sans un mot, le poll tourne
                                         # toutes les 25 s et noierait le journal
        if action == "record":
            tracked[name] = {"at": _remote._now(), "fp": _coverage_fingerprint(name)}
            changed = 1
            log(f"• Auto-apply envoi « {name} » : déjà annoté — rien à réécrire.")
            continue
        try:
            if wdb is None:
                log("Chargement du catalogue BrickStore (poids)…")
                from .weightdb import WeightDB
                wdb = WeightDB()
            apply(name, wdb=wdb, backup=backup, log=log)
            done += 1
            tracked[name] = {"at": _remote._now(), "fp": _coverage_fingerprint(name)}
            changed = 1
        except Exception as e:
            log(f"⚠ envoi « {name} » non appliqué : {str(e)[:120]}")
    if changed:
        _remote._write_json(BATCH_APPLY_FILE, tracked)
    return done


def mark_phase(sets, phase, log=None):
    """Pose la meme phase sur plusieurs sets dans set_status.json — le fichier que le GUI et
    le telephone lisent tous les deux.

    Les cases de sacs cochees sont CONSERVEES : elles disent ce qui a ete fait, et un envoi
    qui revient en arriere (maitre reconstruit) ne doit pas effacer le travail de tri."""
    from . import remote as _remote
    state = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    touched = []
    for s in sets:
        key = str(s)
        entry = state.get(key) or {}
        if entry.get("status") != phase:
            entry["status"] = phase
            state[key] = entry
            touched.append(key)
    if touched:
        _remote._write_json(config.SET_STATUS_FILE, state)
        if log:
            log(f"   phase « {phase} » : {', '.join(touched)}")
    return touched


def build_cfb(name, out_dir=None, log=print):
    """Un seul .bsx pour tout le batch — ce que le client a demandé. Il atterrit dans le
    dossier du batch, à côté du fichier de consolidation."""
    from . import finalize
    res = finalize.build_cfb_files(folders(name),
                                   out_dir=out_dir or batch_dir(name, create_it=True),
                                   log=log, name=str(name))
    # Le maitre est construit : les sets sont emballes et n'attendent plus que le transporteur.
    # C'est l'etape que rien ne marquait — on ne savait plus, en regardant la liste, ce qui
    # etait pret a partir et ce qui restait a trier.
    mark_phase(get(name).get("sets", []), config.STATUS_PRET, log=log)
    return res


def finish(name, remove_stock=True, trash_sets=True, log=print):
    """Le batch est parti : on solde ses sets et on range.

    C'est le « Traiter le set » du flux par set, à l'échelle de l'envoi :

      1. chaque set du batch sort du stock (`history.send_to_cfb`, idempotent et réversible
         en supprimant sa ligne `sent`) ;
      2. chaque DOSSIER DE SET part à la corbeille — récupérable, `trash.py` ne fait jamais
         de suppression définitive ;
      3. le batch est fermé.

    Ce qui reste : le dossier du batch avec le **maître** et le **fichier de consolidation**.
    C'est la trace de ce qui a été expédié, on ne la jette pas. Les sets, eux, sont vides de
    sens une fois le maître construit — comme dans le flux par set.

    L'ordre compte : le stock d'abord, parce qu'il est réversible et qu'un échec y laisse
    tous les fichiers intacts ; les dossiers ensuite, seule étape sans retour hors corbeille.
    """
    from . import history, trash
    from . import remote as _remote
    b = get(name)
    sets = list(b.get("sets", []))
    removed, trashed, kept = 0, [], []

    if remove_stock:
        h = history.HistoryDB()
        try:
            for s in sets:
                n = h.send_to_cfb(s)
                removed += n
                if n:
                    log(f"   inventaire : {n} copie(s) de {s} retirée(s)")
        finally:
            h.close()

    if trash_sets:
        for s in sets:
            f = folder_of(s)
            if not f:
                continue
            try:
                trash.send_to_recycle_bin(f)
                trashed.append(s)
                log(f"   dossier {os.path.basename(f)} → corbeille")
                _remote._prune_phone_files(s)
            except Exception as e:
                kept.append(s)
                log(f"   ⚠ {os.path.basename(f)} NON supprimé : {e}")

    _mark_finished(name)
    log(f"✔ Batch « {name} » terminé : {removed} copie(s) hors stock, "
        f"{len(trashed)} dossier(s) à la corbeille"
        + (f", {len(kept)} gardé(s)" if kept else "")
        + f". Le maître et la consolidation restent dans {batch_dir(name)}.")
    return {"batch": name, "sets": sets, "removed": removed,
            "trashed": trashed, "kept": kept, "dir": batch_dir(name)}


# --- CLI ----------------------------------------------------------------------

def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8")      # ✔ / ⚠ / → sur une console cp1252
    ap = argparse.ArgumentParser(description="Traiter plusieurs sets comme un seul envoi.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    c = sub.add_parser("create"); c.add_argument("name"); c.add_argument("sets", nargs="*")
    a = sub.add_parser("add"); a.add_argument("name"); a.add_argument("set")
    r = sub.add_parser("remove"); r.add_argument("name"); r.add_argument("set")
    s = sub.add_parser("status"); s.add_argument("name")
    pl = sub.add_parser("plan"); pl.add_argument("name")
    fc = sub.add_parser("forecast", help="Phase A depuis le catalogue, avant l'arrivée des sets")
    fc.add_argument("name")
    fc.add_argument("--copies", action="append", default=[], metavar="SET=N",
                    help="forcer le ×N d'un set (sinon: table inventory / Journal)")
    fc.add_argument("--dry-run", action="store_true", help="afficher sans ranger")
    ap_ = sub.add_parser("apply"); ap_.add_argument("name")
    cf = sub.add_parser("cfb"); cf.add_argument("name")
    co = sub.add_parser("conso"); co.add_argument("name")
    cl = sub.add_parser("close"); cl.add_argument("name")
    fi = sub.add_parser("finish"); fi.add_argument("name")
    args = ap.parse_args()

    if args.cmd == "list" or not args.cmd:
        d = all_batches()
        if not d:
            print("aucun batch.")
            return
        for n in sorted(d):
            st = status(n)
            detail = ""
            if st["pieces_expected"]:
                detail = "  (dont %d en route : %s)" % (st["pieces_expected"],
                                                        ", ".join(st["pending"]))
            print("  %-16s %2d set(s)  %6d / %d pièces%s %s%s"
                  % (n, len(st["sets"]), st["pieces"], st["target"], detail,
                     "PRÊT" if st["full"] else "", "  (fermé)" if st["closed"] else ""))
            if st["full"] and not st["closed"]:
                print("      → l'envoi est complet : "
                      "python -m sortpack.batch close %s" % n)
            fc = load_forecast(n)
            if fc:
                s = fc.get("stats") or {}
                print("      plan prévisionnel du %s : %d Sacs, %d pièces%s"
                      % ((fc.get("generated") or "?")[:10], s.get("sacs", 0),
                         s.get("pieces", 0),
                         "  ⚠ PÉRIMÉ" if fc.get("drift") else ""))
            for row in st["sets"]:
                print("      %-8s %6d pièces  %2d sacs%s"
                      % (row["set"], row["pieces"], row["bags"],
                         "" if row["folder"] else "   DOSSIER INTROUVABLE"))
    elif args.cmd == "create":
        create(args.name, args.sets)
        print("batch « %s » créé avec %s" % (args.name, ", ".join(args.sets) or "aucun set"))
    elif args.cmd == "add":
        add(args.name, args.set); print(status(args.name)["pieces"], "pièces dans le batch")
    elif args.cmd == "remove":
        remove(args.name, args.set); print("retiré")
    elif args.cmd == "status":
        st = status(args.name)
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif args.cmd == "plan":
        p = plan(args.name)
        s = p["stats"]
        print("Batch « %s » : %d fichiers, %d lots, %.2f kg -> %d Sacs"
              % (args.name, s["bags"], s["unique_parts"], s["total_weight_g"]/1000.,
                 s["sacs"]))
        print("  consolidation : %d couleurs en C, %d moules multi-couleurs"
              % (s["consolidated_parts"], s["multi_colour_moulds"]))
        cp = consolidation_pass(p)
        print("  passe finale : %d sous-sacs sur %d Sacs"
              % (sum(len(x["sous_sacs"]) for x in cp), len(cp)))
    elif args.cmd == "forecast":
        copies = {}
        for spec in args.copies:
            k, _, v = spec.partition("=")
            copies[k.strip()] = int(v or 0)
        forecast(args.name, copies=copies, save=not args.dry_run)
    elif args.cmd == "apply":
        apply(args.name)
    elif args.cmd == "cfb":
        res = build_cfb(args.name)
        print("Maître : %s (%d lots, %d pièces)"
              % (res["master_path"], res["master_lots"], res["master_qty"]))
    elif args.cmd == "conso":
        res = build_consolidation_bsx(args.name)
        print("%d sous-sacs sur %d Sacs -> %s"
              % (res["groups"], res["sacs"], res["path"]))
    elif args.cmd == "finish":
        st = status(args.name)
        print("Terminer « %s » : %d set(s), %d pièces." % (args.name, len(st["sets"]),
                                                           st["pieces"]))
        print("  • les copies sortent de l'inventaire")
        print("  • les dossiers de sets partent à la corbeille (récupérable)")
        print("  • le maître et la consolidation restent dans %s" % batch_dir(args.name))
        if input("Confirmer ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
            print("annulé"); return
        finish(args.name)
    elif args.cmd == "close":
        close(args.name); print("fermé")


if __name__ == "__main__":
    main()
