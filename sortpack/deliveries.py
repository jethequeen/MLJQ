# -*- coding: utf-8 -*-
"""
Les LIVRAISONS — faire sortir un set de « En attente de livraison » tout seul.

D'où vient l'information
------------------------
`appscript/SortPack_deliveries.gs` scrute la boîte Gmail toutes les heures et range
`deliveries.json` dans le dossier CFB. Deux listes, parce que les deux courriels ne disent
pas la même chose :

* **Amazon « Expédié : 6 « LEGO Icons Jaguar E-Type 11381 » »** — dit QUOI et COMBIEN, mais
  jamais que le colis est arrivé ;
* **Intelcom « Hourra! Votre colis est arrivé! »** — dit QUE c'est arrivé, jamais QUOI. Son
  numéro de suivi (`INTLCM…`) n'apparaît dans aucun courriel d'Amazon, donc il ne relie rien.

L'appariement est donc une INFÉRENCE, et elle est assumée comme telle : **une livraison
constatée solde l'expédition en attente la plus ancienne** (FIFO). C'est juste tant que les
colis arrivent dans l'ordre où ils sont partis, et faux le jour où deux se croisent.

Ce qui en découle, et qu'il faut savoir
---------------------------------------
* **Tous les colis ne passent pas par Intelcom.** Un set livré par un autre transporteur ne
  produira jamais de courriel « arrivé » : il restera « En attente de livraison » jusqu'à ce
  qu'on le débloque à la main. L'inférence ne peut pas inventer ce qu'elle ne reçoit pas ;
* **Amazon scinde les commandes.** Les 10 Jaguar sont partis en 6 + 4 : deux expéditions pour
  un seul achat. La première livraison fait donc passer le set à « Cataloguer » alors qu'une
  partie est encore en route — c'est voulu, on catalogue ce qu'on a ;
* rien n'est jamais *reculé* : une phase déjà avancée (tri, prêt, livré) n'est pas touchée.
"""

import os
import io
import json
import datetime

from . import config

DELIVERIES_FILE = config.state_path("deliveries.json")
# Les appariements déjà faits, pour qu'une livraison ne solde pas deux fois et qu'on puisse
# relire après coup ce qui a été déduit de quoi.
MATCHES_FILE = config.state_path("deliveries_matched.json")


def _read(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def feed():
    """Ce que le scan Gmail a vu : {generated, shipments, deliveries}. Vide s'il n'a jamais
    tourné — auquel cas tout le reste est un no-op, et c'est très bien."""
    d = _read(DELIVERIES_FILE, {}) or {}
    d.setdefault("shipments", [])
    d.setdefault("deliveries", [])
    return d


def _lane(rec):
    """Le circuit d'un enregistrement — qui expédie, qui livre.

    L'appariement est une INFÉRENCE : une livraison constatée solde l'expédition en attente
    la plus ancienne. Tant qu'il n'y avait qu'un circuit (Amazon expédie, Intelcom livre),
    un seul FIFO suffisait. Depuis Costco, non : son courriel de commande attend une
    livraison Costco, et sans cloisonnement le premier colis Amazon livré l'aurait soldée.

    Les enregistrements d'avant n'ont pas le champ — ils viennent tous d'Amazon, d'où le
    défaut."""
    return rec.get("lane") or "amazon"


def match(feed_data=None, matched=None):
    """Apparie les livraisons constatées aux expéditions en attente, du plus ancien au plus
    récent. Rend (nouveaux_appariements, état_complet).

    Un appariement est gardé par l'ID du message de livraison : relancer ne solde pas deux
    fois la même, et on peut relire plus tard ce qui a été déduit de quoi."""
    data = feed_data or feed()
    state = matched if matched is not None else _read(MATCHES_FILE, {}) or {}
    done_delivery_ids = {m["delivery_id"] for m in state.get("matches", [])}
    used_shipments = {m["shipment_id"] for m in state.get("matches", [])}

    pending = [s for s in data["shipments"] if s.get("id") not in used_shipments]
    pending.sort(key=lambda s: s.get("at") or "")
    new = []
    for d in sorted(data["deliveries"], key=lambda x: x.get("at") or ""):
        if d.get("id") in done_delivery_ids:
            continue
        # Une livraison ne peut solder qu'une expédition PARTIE AVANT elle, et venue du
        # MÊME circuit (voir _lane).
        cand = next((s for s in pending
                     if _lane(s) == _lane(d) and (s.get("at") or "") <= (d.get("at") or "")),
                    None)
        if cand is None:
            continue
        pending.remove(cand)
        new.append({"delivery_id": d.get("id"), "delivered_at": d.get("at"),
                    "tracking": d.get("tracking", ""),
                    "shipment_id": cand.get("id"), "shipped_at": cand.get("at"),
                    "set": cand.get("set"), "qty": cand.get("qty")})
    state.setdefault("matches", []).extend(new)
    state["generated"] = datetime.datetime.now().isoformat(timespec="seconds")
    state["pending"] = [{"set": s.get("set"), "qty": s.get("qty"), "shipped_at": s.get("at"),
                         "lane": _lane(s), "eta": s.get("eta") or ""}
                        for s in pending]
    return new, state


def settle(log=None):
    """Fait passer de « En attente de livraison » à « Cataloguer » les sets dont un colis
    vient d'être livré. Rend la liste des sets débloqués.

    N'avance JAMAIS une phase déjà plus loin : on ne ramène pas en cataloguage un set qu'on
    est en train de trier parce qu'un deuxième colis du même achat vient d'arriver."""
    log = log or (lambda _m: None)
    from . import remote as _remote
    data = feed()
    if not data["shipments"] and not data["deliveries"]:
        return []
    new, state = match(data)
    if not new:
        _write(MATCHES_FILE, state)
        return []

    status = _remote._read_json(config.SET_STATUS_FILE, {}) or {}
    freed = []
    for m in new:
        key = str(m["set"])
        entry = status.get(key) or {}
        if entry.get("status") != config.STATUS_LIVRAISON:
            continue                      # jamais en attente, ou déjà plus loin : on ne touche pas
        entry.pop("status", None)         # plus de phase posée → auto_status reprend la main
        if entry:
            status[key] = entry
        else:
            status.pop(key, None)
        freed.append(key)
        log("%s : colis livré le %s%s → cataloguage"
            % (key, (m["delivered_at"] or "")[:10],
               (" (suivi %s)" % m["tracking"]) if m["tracking"] else ""))
    if freed:
        _remote._write_json(config.SET_STATUS_FILE, status)
    _write(MATCHES_FILE, state)
    return freed


def report(log=print):
    """Ce que le suivi sait aujourd'hui — et ce qu'il ne peut pas savoir."""
    data = feed()
    if not data.get("generated"):
        log("Aucun scan Gmail : ajoute SortPack_deliveries.gs au projet Apps Script et "
            "déclenche scanDeliveries toutes les heures.")
        return
    log("Scan du %s — %d expédition(s), %d livraison(s) constatée(s)"
        % (data["generated"][:16], len(data["shipments"]), len(data["deliveries"])))
    _new, state = match(data)
    for m in state.get("matches", [])[-8:]:
        log("   %-8s ×%-3s parti le %s → livré le %s %s"
            % (m["set"], m["qty"], (m["shipped_at"] or "")[:10],
               (m["delivered_at"] or "")[:10], m["tracking"]))
    if state.get("pending"):
        log("   encore en route (aucune livraison constatée) :")
        today = datetime.date.today().isoformat()
        for s in state["pending"]:
            ligne = ("      %-8s ×%-3s parti le %s"
                     % (s["set"], s["qty"], (s["shipped_at"] or "")[:10]))
            eta = s.get("eta") or ""
            if eta:
                # Costco annonce une date : passée sans courriel de livraison, c'est à
                # confirmer à la main — aucun transporteur ne nous le dira.
                ligne += ("   attendu le %s%s"
                          % (eta, "  ⚠ date passée, à confirmer" if eta < today else ""))
            elif _lane(s) != "amazon":
                ligne += "   (aucune date annoncée)"
            log(ligne)


def main():
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    report()
    freed = settle(log=print)
    print("sets débloqués : %s" % (", ".join(freed) if freed else "aucun"))


if __name__ == "__main__":
    main()
