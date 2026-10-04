# -*- coding: utf-8 -*-
"""
Tiny desktop interface for the Sort-and-Pack Helper.

Run it after finishing the cataloguage: pick the set, hit Preview to see the plan,
then Apply to write the remarks into that set's bag files. No BrickStore needed.

    python gui.py      (or double-click "Sort and Pack.bat")
"""

import os
import re
import json
import queue
import datetime
import traceback
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, simpledialog

from sortpack import config
from sortpack import finalize
from sortpack import restants
from sortpack import setvalue
from sortpack.weightdb import WeightDB
from sortpack.catalogdb import CatalogDB
from sortpack.priceguide import PriceGuide, METRIC_ORDER, METRIC_LABELS
from sortpack.plan import build_plan, is_set_folder, bag_sort_key, iter_set_folders
from sortpack.apply import apply_plan
from sortpack import verify
from sortpack import bsx

_WDB = None  # loaded lazily (reads the 62 MB catalog once)
_CAT = None  # BrickStore catalog blob, parsed once (sets -> parts)
_PG = None   # cached price guide (sqlite)


def get_wdb(status):
    global _WDB
    if _WDB is None:
        status("Loading BrickStore catalog (weights)…")
        _WDB = WeightDB()
        status("Catalog loaded.")
    return _WDB


def get_catalog(status):
    global _CAT
    if _CAT is None:
        status("Lecture du catalogue BrickStore (sets → pièces)…")
        _CAT = CatalogDB()
        status("Catalogue chargé.")
    return _CAT


def get_pg(status):
    global _PG
    if _PG is None:
        status("Ouverture du guide de prix…")
        _PG = PriceGuide()
    return _PG


def _batch_map_all():
    """{numero de set: nom du batch} — fermes INCLUS et sans parentheses.

    L'historique ne parle que de sets livres, donc tous leurs batchs sont fermes : la nuance
    « ouvert / ferme » n'y apprend rien, le nom suffit."""
    try:
        from sortpack import batch
        out = {}
        for name, b in batch.all_batches().items():
            for s in b.get("sets", []):
                out[str(s)] = str(name)
        return out
    except Exception:
        return {}


def _catalog_names(set_ids):
    """{numero: nom du set} depuis le catalogue. Le dossier d'un set livre n'existe plus,
    donc son nom ne peut plus venir de la. Best-effort : sans catalogue, on affichera l'id."""
    out = {}
    try:
        from sortpack import catalogdb
        cat = catalogdb.shared()
        for sid in set_ids:
            num = str(sid).split("-")[0]
            if num in out:
                continue
            info = cat.set_info(num)
            if info:
                out[num] = info.name
    except Exception:
        pass
    return out


def _batch_map():
    """{numero de set: etiquette d'envoi} pour la colonne Batch de la liste.

    Un envoi FERME est montre entre parentheses plutot que masque : ses sets existent encore,
    on les trie peut-etre en ce moment meme, et « aucun batch » serait un mensonge. Il
    disparait pour de bon au « Terminer », qui envoie les dossiers a la corbeille.

    Lu une fois par refresh, et sans jamais lever : un registre illisible ne doit pas
    empecher le GUI de s'afficher."""
    try:
        from sortpack import batch
        out = {}
        for name, b in batch.all_batches().items():
            label = f"({name})" if b.get("closed") else str(name)
            for s in b.get("sets", []):
                out[str(s)] = label
        return out
    except Exception:
        return {}


def set_number_of(folder_name):
    """Leading BrickLink set number in a cataloguage folder name, e.g.
    '76342 - Daily Bugle' -> '76342'. Returns None if the name doesn't start
    with a set number."""
    m = re.match(r"\s*(\d{2,7}(?:-\d+)?)", folder_name or "")
    return m.group(1) if m else None


# --- Per-set workflow status -------------------------------------------------
# The list shows, next to each set, the next action to do for it. The Cataloguer↔Ouverture
# split is derived from the files on disk (how empty the Inventory file is); "Trier" and its
# per-bag progress are driven by hand, remembered in config.SET_STATUS_FILE keyed by set
# number as {"status": "Trier", "bags_done": [1, 2]}.

# Colour per status category (kept subtle; readable on the Windows default theme).
_STATUS_COLORS = {
    "cataloguer": "#656d76",   # gray   — cataloguing not finished
    "separer": "#0969da",      # blue   — sachets à répartir par numéro, avant tout
    "trier": "#bc4c00",        # orange — sorting in progress
    "trie": "#1a7f37",         # green  — every bag sorted
    "livraison": "#57606a",    # slate  — bought, still with the carrier
    "attente": "#9a6700",      # amber  — the shipment is not closed yet: cannot sort
    "pret": "#8250df",         # purple — master built, waiting for the carrier
}


def _status_key(folder_name):
    """Stable key for a set's saved state: its set number, else the folder name."""
    return set_number_of(folder_name) or folder_name


def _load_state():
    """The saved workflow state: {set_key: {"status": str, "bags_done": [int]}}."""
    try:
        with open(config.SET_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data):
    try:
        with open(config.SET_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _bought_dates():
    """{numero de set: date d'achat} depuis la table `bought`. La PREMIERE date l'emporte si
    un set a ete rachete : ce qui compte pour la file, c'est depuis quand il attend.

    Best-effort — une base illisible ne doit pas vider la liste du GUI."""
    try:
        from sortpack.history import HistoryDB
        h = HistoryDB()
        try:
            rows = h._conn.execute(
                "SELECT set_id, MIN(bought_date) FROM bought GROUP BY set_id").fetchall()
        finally:
            h.close()
        return {str(s).split("-")[0]: (d or "") for s, d in rows}
    except Exception:
        return {}


def _in_open_batch(number):
    """Ce set appartient-il à un envoi PAS ENCORE FERMÉ ? Best-effort."""
    try:
        from sortpack import batch as _batch
        return bool(_batch.batch_of(number))
    except Exception:
        return False


def _rest_of_batch_pending(number):
    """Les autres sets de l'envoi qui ne sont pas encore finis, ou None. Best-effort : un
    registre illisible ramène simplement à l'ancien affichage (« Trié ✓ »)."""
    try:
        from sortpack import batch as _batch
        res = _batch.rest_pending(number)
        return res[1] if res else None
    except Exception:
        return None


def _batch_of_set(number):
    """Le nom de l'envoi qui possède ce set (fermé compris), ou "". Best-effort."""
    if not number:
        return ""
    try:
        from sortpack import batch as _batch
        return _batch.owning_batch(number) or ""
    except Exception:
        return ""


def build_index():
    """The per-set snapshot the phone reads: name, key, number, base status, bag count, and
    the flags that drive the phone's button gating — has_inventory and restants_done (does
    the leftovers Inventory file carry remarks written by 'Calculer les restants')."""
    root = config.CATALOGUAGE_ROOT
    out = []
    if not os.path.isdir(root):
        return out
    from sortpack.plan import find_inventory
    dates = _bought_dates()
    for folder in iter_set_folders(root):
        name = os.path.basename(folder)
        inv = find_inventory(folder)
        num = set_number_of(name) or ""
        out.append({
            "name": name, "key": _status_key(name), "folder": folder,
            "number": num,
            "bought_date": dates.get(num, ""),
            # Le téléphone reçoit la phase DE TRAVAIL : un set d'envoi fermé dont les sacs
            # faits sont annotés se trie, même si sa boîte n'est pas finie de cataloguer.
            # `cataloguing` dit le reste, pour que la page puisse le signaler.
            "base_status": (config.STATUS_TRIER
                            if (auto_status(folder) == config.STATUS_CATALOGUER
                                and sortable_mid_catalogue(folder))
                            else auto_status(folder)),
            "cataloguing": auto_status(folder) == config.STATUS_CATALOGUER,
            # Pastille « Séparer les sacs » : geste manuel, ou déjà évident si des sacs
            # numérotés existent (on n'aurait pas pu les cataloguer sans séparer).
            "separated": bool((_load_state().get(_status_key(name)) or {}).get("separated")
                              or bag_count(folder)),
            # L'envoi auquel ce set appartient, ou "". Le téléphone en a besoin pour ne pas
            # proposer un « Construire CFB » par set : un set d'envoi part avec tout
            # l'envoi, et le PC refusait déjà la demande (remote._do_build_cfb) — mais
            # après coup, une fois le bouton pressé.
            "batch": _batch_of_set(num),
            # Envoi pas encore fermé : la numérotation des Sacs peut changer, donc on ne
            # trie pas. Le téléphone en a besoin pour afficher l'attente sans attendre que
            # settle_phases ait posé la phase.
            "batch_open": _in_open_batch(num),
            "bag_count": bag_count(folder),
            "has_inventory": bool(inv),
            "restants_done": bool(inv and bsx.has_remarks(inv)),
        })
    # Dans l'ordre ou on les a achetes : c'est l'ordre dans lequel ils arrivent, donc celui
    # dans lequel on les traite. Un set sans achat enregistre (stock migre) passe en dernier
    # plutot qu'en tete — faute de date, il n'a rien a dire sur sa place dans la file.
    out.sort(key=lambda e: (e["bought_date"] == "", e["bought_date"], e["name"]))
    return out


def _refresh_mobile(action=None):
    """Push a fresh dashboard.json to the phone after a desktop accounting action, so the
    Finances tab matches what you just did instead of waiting for the poll. Best-effort."""
    try:
        from sortpack import mobile
        if action:
            mobile.mark_run(action)
        mobile.write_dashboard()
    except Exception:
        pass


def _write_sort_index(sets):
    """Write the phone-facing snapshot (config.SORT_INDEX_FILE). Best-effort — a Drive
    hiccup here must never break the desktop refresh."""
    try:
        payload = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "sets": sets}
        with open(config.SORT_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _bsx_files(folder):
    try:
        return [n for n in os.listdir(folder) if n.lower().endswith(".bsx")]
    except OSError:
        return []


def _is_inventory(name):
    return any(name.startswith(p) for p in config.SKIP_PREFIXES)


def catalogue_totals(folder):
    """(inventory_qty, bags_qty): total pieces still in the leftovers Inventory file vs.
    already moved into the numbered bags. Their sum ≈ the set's parts, so the ratio says
    how far cataloguing has gone."""
    inv_qty = bags_qty = 0
    for n in _bsx_files(folder):
        q = bsx.total_qty(os.path.join(folder, n))
        if _is_inventory(n):
            inv_qty += q
        else:
            bags_qty += q
    return inv_qty, bags_qty


def auto_status(folder):
    """Ce que les FICHIERS disent, et rien d'autre : « Cataloguer » tant que l'inventaire de
    restes pèse plus que la done-fraction du set, « Trier » une fois qu'il est quasi vide.
    Un dossier vide est « Cataloguer ».

    Il n'y a plus d'« Ouverture des sacs » entre les deux : l'ouverture fait partie du
    cataloguage maintenant qu'on sépare les sachets AVANT (voir config.STATUS_SEPARER),
    donc le cataloguage débouche directement sur le tri."""
    inv_qty, bags_qty = catalogue_totals(folder)
    base = inv_qty + bags_qty
    if base <= 0:
        return config.STATUS_CATALOGUER
    if inv_qty <= config.CATALOGUE_DONE_FRACTION * base:
        return config.STATUS_TRIER
    return config.STATUS_CATALOGUER


def sortable_mid_catalogue(folder):
    """Ce set est-il triable alors que son cataloguage n'est pas fini ?

    Vrai pour un set d'envoi FERMÉ dont des sacs portent déjà des remarques — voir
    batch.sortable_while_cataloguing pour le pourquoi. Les Sacs de l'envoi sont ouverts sur
    la table et leur numérotation est figée : les premiers sacs d'un set peuvent y tomber
    pendant qu'on catalogue le reste de la boîte. Best-effort : un registre illisible
    ramène simplement à l'ancien comportement (« Cataloguer » jusqu'au bout)."""
    try:
        from sortpack import batch as _batch
        return _batch.sortable_while_cataloguing(folder)
    except Exception:
        return False


def bag_count(folder):
    """Number of physical numbered bags = the BIGGEST bag number among the bag files
    ('8.bsx' or '8 - 1.bsx' → 8). '1' and '1 - 1' are the same bag. 0 if none."""
    biggest = 0
    for n in _bsx_files(folder):
        if _is_inventory(n):
            continue
        kind, main, _sub, _ = bag_sort_key(n)
        if kind == 0:
            biggest = max(biggest, main)
    return biggest


def status_for(name, state, folder=None):
    """(label, colour-category) for a set folder.

    L'ordre des phases : En attente de livraison → Séparer les sacs → Cataloguer ⇄ Trier →
    Trié (ou En attente du reste du batch) → Prêt → Livré. Seules « Cataloguer » et
    « Trier » se chevauchent, et c'est voulu : on annote et on trie les sacs déjà faits
    pendant qu'on catalogue les suivants.

    Ce qui est LU dans set_status.json : les phases qu'aucun fichier ne peut deviner
    (livraison, prêt, séparation faite) et les sacs cochés. Tout le reste est déduit des
    fichiers, pour qu'un drapeau oublié ne fige jamais la liste."""
    # `folder` est fourni par l'index : un set d'un batch vit dans CFB/Batchs/<nom>/, on ne
    # peut plus le deduire du nom. Le repli ne sert qu'aux appels hors liste.
    folder = folder or os.path.join(config.CATALOGUAGE_ROOT, name)
    entry = state.get(_status_key(name)) or {}
    # « Prêt pour livraison » est posé par la construction du maître : le set est emballé,
    # son avancement de tri n'a plus rien à dire. Il passe donc AVANT la lecture des fichiers,
    # sinon un set dont l'inventaire a été vidé retomberait sur « Cataloguer ».
    if entry.get("status") == config.STATUS_PRET:
        return config.STATUS_PRET, "pret"
    if entry.get("status") == config.STATUS_ATTENTE:
        return config.STATUS_ATTENTE, "attente"
    if entry.get("status") == config.STATUS_LIVRAISON:
        return config.STATUS_LIVRAISON, "livraison"
    # Avant tout le reste : les sachets des N boîtes sont-ils répartis en piles par numéro ?
    # Il faut le faire D'ABORD — sachets ouverts et versés, le cataloguage par sac devient
    # impossible. C'est purement physique, donc ça se clôt sur un geste (`separated`) ; mais
    # un set qui a déjà des sacs numérotés l'est forcément, et on ne le lui réclame pas.
    if not entry.get("separated") and not bag_count(folder):
        return config.STATUS_SEPARER, "separer"
    cataloguing = auto_status(folder) == config.STATUS_CATALOGUER
    if cataloguing and not sortable_mid_catalogue(folder):
        return config.STATUS_CATALOGUER, "cataloguer"
    # Cataloguage en cours mais sacs déjà annotés (set d'envoi fermé) : on suit l'avancement
    # du TRI comme pour n'importe quel set, en disant que la boîte n'est pas vidée — sinon
    # rien ne distinguerait « 2 sacs sur 2 » de « 2 sacs sur les 12 à venir ».
    suffix = " · cataloguage en cours" if cataloguing else ""
    done_list = entry.get("bags_done") or []
    # Un set d'envoi encore OUVERT ne se trie pas : sa composition peut changer, donc la
    # numérotation des Sacs n'est pas figée. Déduit, pas lu : `settle_phases` écrit la même
    # phase, mais l'affichage ne doit pas dépendre d'un drapeau posé au bon moment.
    if _in_open_batch(_status_key(name)):
        return config.STATUS_ATTENTE, "attente"
    total = bag_count(folder)
    if not total:
        return config.STATUS_TRIER + suffix, "trier"
    done = len([b for b in done_list if 1 <= b <= total])
    # Un set encore en cataloguage n'est jamais « Trié » : d'autres sacs vont arriver, et la
    # coche verte voudrait dire « plus rien à faire » sur une boîte à moitié vidée.
    if done >= total and not cataloguing:
        # Trié, mais son envoi ne l'est pas : il n'y a plus rien à y faire et il ne part
        # pas pour autant — le maître couvre tout l'envoi. « Trié ✓ » se lirait « prêt à
        # expédier ».
        rest = _rest_of_batch_pending(_status_key(name))
        if rest:
            return (f"{config.STATUS_ATTENTE_RESTE} ({done}/{total})", "attente")
        return f"{config.STATUS_TRIE} ✓ ({done}/{total})", "trie"
    return f"{config.STATUS_TRIER} ({done}/{total}){suffix}", "trier"


class App:
    def __init__(self, root):
        self.root = root
        self._folder_by_name = {}      # nom du set -> chemin reel (racine ou batch)
        root.title("Sort & Pack Helper")
        root.geometry("880x600")

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Set folder:").pack(side="left")
        ttk.Button(top, text="↻ Refresh", command=self.refresh).pack(side="right")
        ttk.Button(top, text="📂 Ouvrir le dossier",
                   command=self.open_folder).pack(side="right", padx=6)
        ttk.Button(top, text="🗂 Suivi du tri",
                   command=self.open_sort_tracker).pack(side="right")

        # Status bar first (bottom), then a resizable vertical split fills the rest so the
        # set list and the output share space dynamically (drag the divider to taste).
        self.statusbar = ttk.Label(root, text="", anchor="w", relief="sunken")
        self.statusbar.pack(fill="x", side="bottom")

        self._paned = ttk.PanedWindow(root, orient="vertical")
        self._paned.pack(fill="both", expand=True)

        # -- top pane: deux onglets, les sets en cours et l'historique des livres --
        # Un set livre quitte la liste (son dossier est a la corbeille) mais pas la memoire :
        # l'onglet Historique le relit dans la table `sent`, qui est la seule trace qui reste.
        self._tabs = ttk.Notebook(self._paned)
        listpane = ttk.Frame(self._tabs, padding=(8, 4))
        # Each row shows the set's Brickset box art (in the tree's own #0 column), so the row
        # height is bumped to fit a small thumbnail. Thumbs load off the UI thread and cache.
        self._thumb = 40
        self._img_cache = {}        # set-id -> PhotoImage (kept alive so Tk keeps them)
        self._img_loading = set()   # set-ids with an in-flight download (dedupe)
        self._img_queue = queue.Queue()   # worker threads hand decoded images back here
        style = ttk.Style()
        style.configure("Sets.Treeview", rowheight=self._thumb + 8)
        # La colonne #0 d'un Treeview porte l'image et vient toujours en premier : c'est donc
        # elle qui prend la DATE D'ACHAT, pour l'avoir a gauche du nom comme demande. Le nom
        # descend dans une colonne normale, nettement plus etroite qu'avant — il occupait 600
        # pixels pour rien.
        # #0 ne porte QUE la vignette : la colonne image d'un Treeview vient toujours en
        # premier, et y mettre la date la collait a gauche du nom. La date a donc sa propre
        # colonne, a droite du « numero - nom ».
        self.tree = ttk.Treeview(listpane, columns=("set", "achete", "status", "batch"),
                                 show="tree headings", height=4, selectmode="browse",
                                 style="Sets.Treeview")
        self.tree.heading("#0", text="")
        self.tree.heading("set", text="Set")
        self.tree.heading("achete", text="Acheté")
        self.tree.heading("status", text="Prochaine action")
        self.tree.column("#0", width=60, anchor="center", stretch=False)
        self.tree.column("set", width=300, anchor="w")
        self.tree.column("achete", width=100, anchor="center", stretch=False)
        self.tree.column("status", width=170, anchor="w", stretch=False)
        # L'envoi auquel le set appartient, en colonne plutôt qu'en suffixe « [1] » collé à
        # la prochaine action : c'est une propriété du set, pas une étape de son avancement,
        # et une colonne se lit du coin de l'œil sur toute la liste.
        self.tree.heading("batch", text="Batch")
        self.tree.column("batch", width=70, anchor="center", stretch=False)
        for tag, color in _STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(listpane, command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.config(yscrollcommand=sb.set)
        self.tree.bind("<Double-Button-1>", lambda e: self.preview())
        self.tree.bind("<Button-3>", self._show_set_menu)
        self._tabs.add(listpane, text="Sets")

        # -- onglet Historique : les sets livres, meme presentation --
        histpane = ttk.Frame(self._tabs, padding=(8, 4))
        self.htree = ttk.Treeview(histpane, columns=("set", "livre", "qte", "batch"),
                                  show="tree headings", height=4, selectmode="browse",
                                  style="Sets.Treeview")
        self.htree.heading("#0", text="")
        self.htree.heading("set", text="Set")
        self.htree.heading("livre", text="Livré le")
        self.htree.heading("qte", text="Copies")
        self.htree.heading("batch", text="Batch")
        self.htree.column("#0", width=60, anchor="center", stretch=False)
        self.htree.column("set", width=300, anchor="w")
        self.htree.column("livre", width=100, anchor="center", stretch=False)
        self.htree.column("qte", width=70, anchor="center", stretch=False)
        self.htree.column("batch", width=70, anchor="center", stretch=False)
        self.htree.pack(side="left", fill="both", expand=True)
        hsb = ttk.Scrollbar(histpane, command=self.htree.yview)
        hsb.pack(side="right", fill="y")
        self.htree.config(yscrollcommand=hsb.set)
        self._tabs.add(histpane, text="Historique")
        self._paned.add(self._tabs, weight=1)

        # -- bottom pane: the action buttons + the output/preview area --
        lower = ttk.Frame(self._paned)

        g1 = ttk.LabelFrame(lower, text="Set sélectionné", padding=(8, 4))
        g1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Button(g1, text="Aperçu", command=self.preview).pack(side="left")
        ttk.Button(g1, text="Appliquer (écrire les remarques)",
                   command=self.apply).pack(side="left", padx=6)
        ttk.Button(g1, text="Construire fichier CFB",
                   command=self.build_cfb).pack(side="left", padx=6)
        ttk.Button(g1, text="Vérifier les quantités",
                   command=self.verify_set).pack(side="left", padx=6)
        ttk.Button(g1, text="Valeur du set",
                   command=self.value_set).pack(side="right")
        self.backup_var = tk.BooleanVar(value=config.ENABLE_BACKUP)
        ttk.Checkbutton(g1, text="Sauvegarder d'abord",
                        variable=self.backup_var).pack(side="right", padx=6)

        # Le batch : plusieurs sets expédiés en un seul envoi (voir sortpack/batch.py).
        # Onze boutons vivaient ici et mangeaient la fenêtre alors qu'on s'en sert une fois
        # par envoi. Tout est passé dans « Gérer les batchs » ; seul « Appliquer » reste à
        # portée, parce que celui-là se clique à chaque fois qu'un set avance.
        gb = ttk.LabelFrame(lower, text="Batch — plusieurs sets en un seul envoi",
                            padding=(8, 4))
        gb.pack(fill="x", padx=8, pady=(2, 2))
        self.batch_var = tk.StringVar()      # le batch courant : plus de combobox, mais tous
                                             # les gestes de batch le lisent toujours
        ttk.Button(gb, text="Gérer les batchs…",
                   command=self.batch_manage).pack(side="left")
        ttk.Button(gb, text="Appliquer le batch du set sélectionné",
                   command=self.batch_apply_selected).pack(side="left", padx=8)
        self.batch_lbl = ttk.Label(gb, text="")
        self.batch_lbl.pack(side="right")

        g2 = ttk.LabelFrame(lower, text="MLJQ — achats & comptabilité", padding=(8, 4))
        g2.pack(fill="x", padx=8, pady=(2, 6))
        ttk.Button(g2, text="J'ai acheté un set",
                   command=self.log_purchase).pack(side="left")
        ttk.Button(g2, text="Synchroniser les achats",
                   command=self.sync_purchases).pack(side="left", padx=6)
        ttk.Button(g2, text="Rapport d'achat",
                   command=lambda: self.run_buy_report(post=True)).pack(side="left", padx=6)
        ttk.Button(g2, text="Inventaire CFB",
                   command=self.inventory_cfb).pack(side="left", padx=6)
        ttk.Button(g2, text="Fin de mois",
                   command=self.end_of_month).pack(side="left", padx=6)
        ttk.Button(g2, text="Nouveaux cookies",
                   command=self.new_cookies).pack(side="left", padx=6)
        ttk.Button(g2, text="📱 Demandes",
                   command=lambda: self.process_remote(manual=True)).pack(side="left", padx=6)

        self.output = tk.Text(lower, height=8, wrap="none")
        self.output.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self._paned.add(lower, weight=3)

        self.refresh()
        self.root.after(150, self._drain_img_queue)   # main-thread painter for thumbnails
        # Place the divider once the window has a real size: ~1 row per set (min 2), so the
        # list takes only what it needs and the output/preview gets the rest.
        self.root.after(60, self._init_sash)
        self._remote_busy = False
        self.root.after(4000, self._poll_remote)       # phone → PC command queue poller

    def _init_sash(self):
        try:
            rows = max(2, len(self.tree.get_children()))
            pos = min(74 + rows * (self._thumb + 8), 320)   # heading + N rows, capped
            self._paned.sashpos(0, pos)
        except Exception:
            pass

    # -- helpers --
    def status(self, msg):
        self.statusbar.config(text=msg)
        self.statusbar.update_idletasks()

    def log(self, msg):
        self.output.insert("end", msg + "\n")
        self.output.see("end")

    def selected_name(self):
        """The folder name of the selected set, or None."""
        sel = self.tree.selection()
        if not sel:
            return None
        return self._row_name(sel[0])

    def selected_folder(self):
        """Le chemin du set choisi. Il vient de l'index, pas d'un os.path.join sur la racine :
        depuis les batchs, un set peut etre dans CFB/Batchs/<nom>/."""
        name = self.selected_name()
        if not name:
            return None
        known = getattr(self, "_folder_by_name", {}).get(name)
        if known and os.path.isdir(known):
            return known
        return os.path.join(config.CATALOGUAGE_ROOT, name)

    def refresh_history(self):
        """L'onglet Historique : les sets livrés, lus dans la table `sent`.

        Pas un balayage du disque — au « Livré », le dossier est à la corbeille. Le nom et le
        batch sont retrouvés après coup : le catalogue donne le nom, le registre dit à quel
        envoi le set appartenait (les batchs fermés y restent). Best-effort des deux côtés :
        un historique ne doit jamais empêcher le GUI de s'ouvrir."""
        self.htree.delete(*self.htree.get_children())
        try:
            from sortpack.history import HistoryDB
            h = HistoryDB()
            try:
                rows = h.delivered()
            finally:
                h.close()
        except Exception as e:
            self.status(f"Historique indisponible : {str(e)[:60]}")
            return
        bmap = _batch_map_all()
        names = _catalog_names([r["set_id"] for r in rows])
        for r in rows:
            num = str(r["set_id"]).split("-")[0]
            nom = names.get(num) or ""
            label = f"{num} - {nom}" if nom else str(r["set_id"])
            img = self._img_cache.get(self._sid_for(label))
            self.htree.insert("", "end", text="", image=(img or ""),
                              values=(label, r["sent_date"], r["qty"], bmap.get(num, "")))
        return len(rows)

    def refresh(self, select_name=None):
        if select_name is None:
            select_name = self.selected_name()   # keep the selection across a refresh
        self.tree.delete(*self.tree.get_children())
        root = config.CATALOGUAGE_ROOT
        if not os.path.isdir(root):
            self.status(f"Cataloguage root not found: {root}")
            return
        try:                       # une phase posée depuis le téléphone est rattrapée ici
            from sortpack import batch as _b, deliveries as _d
            _d.settle(log=self.log)    # un colis livré sort le set de l'attente
            _b.settle_phases(log=self.log)
        except Exception as e:
            self.status(f"Phases non réconciliées : {str(e)[:60]}")
        state = _load_state()
        index = build_index()
        bmap = _batch_map()
        # Un set peut vivre a la racine OU dans un dossier de batch : on ne peut plus
        # reconstruire son chemin a partir de son nom, on le retient.
        self._folder_by_name = {e["name"]: e["folder"] for e in index}
        to_select = None
        for e in index:
            name = e["name"]
            label, tag = status_for(name, state, e["folder"])
            b = bmap.get(str(e.get("number"))) or ""
            img = self._img_cache.get(self._sid_for(name))
            iid = self.tree.insert("", "end", text="",
                                   values=(name, e.get("bought_date") or "—", label, b),
                                   tags=(tag,), image=(img or ""))
            if name == select_name:
                to_select = iid
        if to_select is None:
            # Rien de demande (ouverture du GUI, ou le set selectionne vient de partir) : on
            # prend la premiere ligne. La liste etant triee par date d'achat, c'est le set qui
            # attend depuis le plus longtemps — celui par lequel on veut commencer.
            kids = self.tree.get_children()
            to_select = kids[0] if kids else None
        if to_select:
            self.tree.selection_set(to_select)
            self.tree.focus(to_select)
            self.tree.see(to_select)
        self.refresh_history()        # l'onglet d'à côté suit la même touche ↻
        _write_sort_index(index)      # small snapshot the phone web-app reads
        self._load_row_images()       # fetch any thumbnails not yet cached
        self._batch_refresh()
        self.status(f"{len(index)} sets found.")

    # ------------------------------------------------------------------ batchs
    def batch_apply_selected(self):
        """Applique les remarques du batch AUQUEL APPARTIENT LE SET SÉLECTIONNÉ.

        C'est le seul geste de batch qu'on refait souvent — à chaque fois qu'un set avance —
        donc il reste sur la fenêtre principale. Et il se déduit de la sélection plutôt que
        d'obliger à rouvrir une fenêtre pour redire quel envoi : on vient de cliquer sur le
        set, le batch n'est pas une question."""
        name = self.selected_name()
        if not name:
            messagebox.showinfo("Batch", "Choisis un set dans la liste d'abord.")
            return
        num = set_number_of(name)
        try:
            from sortpack import batch
            bname = batch.batch_of(num)
            if not bname:
                # Fermé ou jamais inscrit : on regarde aussi les fermés, pour pouvoir
                # réécrire les remarques d'un envoi déjà bouclé mais pas encore livré.
                for nm, b in batch.all_batches().items():
                    if str(num) in b.get("sets", []):
                        bname = nm
                        break
            if not bname:
                messagebox.showinfo(
                    "Batch",
                    f"« {name} » n'appartient à aucun envoi.\n\n"
                    f"Utilise « Appliquer (écrire les remarques) » pour un set seul, ou "
                    f"inscris-le à un envoi dans « Gérer les batchs… ».")
                return
            self.batch_var.set(bname)
            self.batch_apply()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def batch_manage(self):
        """La fenêtre des envois : la liste, et tous les gestes qu'on y fait.

        Chaque bouton réutilise la méthode qui existait déjà — ils lisaient tous
        `self.batch_var`, donc il suffit de la poser sur la ligne choisie avant d'appeler."""
        from sortpack import batch
        win = tk.Toplevel(self.root)
        win.title("Gérer les envois")
        win.geometry("760x420")
        win.transient(self.root)

        tv = ttk.Treeview(win, columns=("sets", "pieces", "etat"), show="tree headings",
                          selectmode="browse", height=10)
        tv.heading("#0", text="Envoi")
        tv.heading("sets", text="Sets")
        tv.heading("pieces", text="Pièces")
        tv.heading("etat", text="État")
        tv.column("#0", width=150, anchor="w")
        tv.column("sets", width=240, anchor="w")
        tv.column("pieces", width=140, anchor="center", stretch=False)
        tv.column("etat", width=180, anchor="w", stretch=False)
        tv.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        def reload_rows():
            keep = self.batch_var.get()
            tv.delete(*tv.get_children())
            for nm in sorted(batch.all_batches()):
                try:
                    st = batch.status(nm)
                except Exception as e:
                    tv.insert("", "end", text=nm, values=("", "", str(e)[:40]))
                    continue
                pieces = f"{st['pieces']:,} / {st['target']:,}"
                if st["pieces_expected"]:
                    pieces += f"  (dont {st['pieces_expected']:,} en route)"
                etat = "fermé" if st["closed"] else ("PRÊT" if st["full"] else "en cours")
                iid = tv.insert("", "end", text=nm,
                                values=(", ".join(st["sets"] and
                                                  [r["set"] for r in st["sets"]]) or "—",
                                        pieces, etat))
                if nm == keep:
                    tv.selection_set(iid)
            if not tv.selection():
                kids = tv.get_children()
                if kids:
                    tv.selection_set(kids[0])

        def current():
            sel = tv.selection()
            if not sel:
                messagebox.showinfo("Envois", "Choisis un envoi dans la liste.", parent=win)
                return None
            self.batch_var.set(tv.item(sel[0], "text"))
            return self.batch_var.get()

        def act(fn):
            """Pose le batch choisi puis lance le geste, et recharge la liste après."""
            def run():
                if current() is None:
                    return
                fn()
                reload_rows()
                self._batch_show()
            return run

        tv.bind("<<TreeviewSelect>>", lambda e: current())

        bar1 = ttk.Frame(win); bar1.pack(fill="x", padx=8, pady=2)
        ttk.Button(bar1, text="Nouveau", command=act(self.batch_new)).pack(side="left")
        ttk.Button(bar1, text="+ set", command=act(self.batch_add)).pack(side="left", padx=6)
        ttk.Button(bar1, text="− set", command=act(self.batch_remove)).pack(side="left")
        ttk.Button(bar1, text="Plan prévisionnel",
                   command=act(self.batch_forecast)).pack(side="left", padx=(16, 0))
        ttk.Button(bar1, text="Aperçu", command=act(self.batch_preview)).pack(side="left", padx=6)

        bar2 = ttk.Frame(win); bar2.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(bar2, text="Appliquer le batch",
                   command=act(self.batch_apply)).pack(side="left")
        ttk.Button(bar2, text="Consolidation",
                   command=act(self.batch_consolidation)).pack(side="left", padx=6)
        ttk.Button(bar2, text="Construire le CFB",
                   command=act(self.batch_cfb)).pack(side="left")
        ttk.Button(bar2, text="Fermer l'envoi",
                   command=act(self.batch_close)).pack(side="left", padx=(16, 0))
        ttk.Button(bar2, text="Marquer livré",
                   command=act(self.batch_finish)).pack(side="left", padx=6)
        ttk.Button(bar2, text="Fermer la fenêtre",
                   command=win.destroy).pack(side="right")

        reload_rows()
        return win

    def _batch_refresh(self):
        """Garde `batch_var` sur un batch qui existe encore, et rafraîchit la pastille."""
        try:
            from sortpack import batch
            names = sorted(batch.all_batches())
        except Exception:
            names = []
        if self.batch_var.get() not in names:
            self.batch_var.set(names[0] if names else "")
        self._batch_show()

    def _batch_show(self):
        """La pastille de droite : où en est le batch choisi."""
        name = self.batch_var.get()
        if not name:
            self.batch_lbl.config(text="aucun batch")
            return
        try:
            from sortpack import batch
            st = batch.status(name)
            # Un dossier qui n'a pas pu être déplacé dans l'envoi (Drive ou l'explorateur
            # le tenait ouvert). `settle` réessaie à chaque passe ; c'est juste visible.
            waiting = [r["set"] for r in st["sets"] if not r["in_folder"] and r["folder"]]
            txt = (f"{len(st['sets'])} set(s) · {st['pieces']} / {st['target']} pièces"
                   + (f" (dont {st['pieces_expected']} en route)"
                      if st["pieces_expected"] else "")
                   + ("  PRÊT" if st["full"] else "")
                   + (f"  · {len(waiting)} dossier(s) hors du dossier d'envoi"
                      if waiting else "")
                   + ("  (fermé)" if st["closed"] else ""))
            # Un set acheté mais pas encore livré n'a pas de dossier — c'est normal, pas un
            # avertissement. Seul un set sans dossier ET sans achat enregistré en est un :
            # là, personne ne sait ce qu'il apportera.
            unknown = [s for s in st["missing"] if s not in st["pending"]]
            if unknown:
                txt += f"  ⚠ ni dossier ni achat : {', '.join(unknown)}"
            self.batch_lbl.config(text=txt)
        except Exception as e:
            self.batch_lbl.config(text=str(e)[:60])

    def _batch_name(self):
        name = self.batch_var.get()
        if not name:
            messagebox.showinfo("Batch", "Crée ou choisis un batch d'abord.")
            return None
        return name

    def batch_new(self):
        from tkinter import simpledialog
        from sortpack import batch
        name = simpledialog.askstring("Nouveau batch", "Nom du batch :", parent=self.root)
        if not name:
            return
        try:
            batch.create(name.strip())
            self.batch_var.set(name.strip())
            self._batch_refresh()
            self.status(f"Batch « {name.strip()} » créé — ajoute-lui des sets.")
        except Exception as e:
            messagebox.showerror("Batch", str(e))

    def batch_add(self):
        """Ajoute le set sélectionné dans la liste au batch courant."""
        from sortpack import batch
        name = self._batch_name()
        folder = self.selected_folder()
        if not name or not folder:
            if name:
                messagebox.showinfo("Choisir un set", "Sélectionne un set dans la liste.")
            return
        try:
            batch.add(name, set_number_of(os.path.basename(folder)))
            self.refresh()
            st = batch.status(name)
            self.status(f"Batch « {name} » : {st['pieces']} / {st['target']} pièces.")
        except Exception as e:
            messagebox.showerror("Batch", str(e))

    def batch_remove(self):
        from sortpack import batch
        name = self._batch_name()
        folder = self.selected_folder()
        if not name or not folder:
            return
        try:
            batch.remove(name, set_number_of(os.path.basename(folder)))
            self.refresh()
        except Exception as e:
            messagebox.showerror("Batch", str(e))

    def batch_close(self):
        """Fermer l'envoi : « plus rien n'entre ». Rien d'autre.

        C'est une DÉCISION, pas une conséquence du compte de pièces — on peut vouloir glisser
        un dernier set en rabais dans un envoi déjà au-delà de la cible, ou au contraire
        renoncer à remplir un envoi incomplet. D'où le bouton plutôt qu'une fermeture
        automatique. Réversible : le même bouton rouvre.

        À ne pas confondre avec « Terminer », qui solde l'envoi une fois parti (les copies
        sortent du stock, les dossiers vont à la corbeille)."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            st = batch.status(name)
            if st["closed"]:
                if messagebox.askyesno(
                        "Rouvrir l'envoi",
                        f"L'envoi « {name} » est fermé.\n\n"
                        f"Le rouvrir pour y remettre des sets ?"):
                    batch.close(name, False)
                    self.log(f"Envoi « {name} » rouvert.")
                    self._batch_show()
                return
            detail = f"{st['pieces']:,} / {st['target']:,} pièces"
            if st["pieces_expected"]:
                detail += (f"\n(dont {st['pieces_expected']:,} achetées mais pas encore "
                           f"reçues : {', '.join(st['pending'])})")
            msg = [f"Fermer l'envoi « {name} » ?", "", detail, ""]
            if st["full"]:
                msg.append("Il est complet. Plus aucun set ne pourra y être ajouté, et le "
                           "prochain classement visera un envoi neuf.")
            else:
                manque = st["target"] - st["pieces"]
                msg.append(f"⚠ Il n'est PAS complet — il manque {manque:,} pièces. "
                           f"Le fermer veut dire que tu renonces à le remplir.")
            msg += ["", "Réversible : le même bouton le rouvrira."]
            if not messagebox.askyesno("Fermer l'envoi", "\n".join(msg),
                                       default="yes" if st["full"] else "no"):
                return
            batch.close(name)
            self.log(f"=== Envoi « {name} » fermé — {detail} ===")
            self.status(f"Envoi « {name} » fermé.")
            self._batch_show()
            _refresh_mobile()          # le téléphone doit le voir sans attendre le poll
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Fermer l'envoi", str(e))

    def batch_forecast(self):
        """La Phase A du batch calculée depuis le CATALOGUE, avant que ses sets soient
        arrivés — pour pouvoir trier le premier set aux numéros de Sacs de l'envoi complet.

        Voir sortpack/forecast.py. Le plan est rangé dans le dossier du batch ; « Appliquer
        le batch » s'y accroche ensuite tout seul."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            started = batch.sorting_started(name)
            msg = ["Calculer le plan prévisionnel du batch « %s » ?" % name,
                   "",
                   "Il fixe la numérotation des Sacs de TOUT l'envoi à partir du catalogue,",
                   "sans attendre que les sets soient arrivés ni catalogués. Les sets déjà",
                   "là se trieront ensuite à ces numéros-là.",
                   "",
                   "Le ×N de chaque set vient du Journal (achats Catégorie A) : un set sans",
                   "achat enregistré compte pour 0 et fausserait tout le plan."]
            if started:
                msg += ["",
                        "⚠ LE TRI EST DÉJÀ ENGAGÉ SUR : %s" % ", ".join(started),
                        "Un nouveau plan leur donnera d'autres Sacs que ceux déjà écrits.",
                        "Ne continue que si rien n'est physiquement en sac."]
            if not messagebox.askyesno("Plan prévisionnel", "\n".join(msg),
                                       default="no" if started else "yes"):
                return
            self.output.delete("1.0", "end")
            self.status("Plan prévisionnel…")
            self.log(f"=== Plan prévisionnel — batch « {name} » ===")
            res = batch.forecast(name, log=self.log)
            st = res["stats"]
            self.status(f"Plan prévisionnel : {st['sacs']} Sacs, {st['pieces']} pièces.")
            self._batch_show()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Plan prévisionnel", str(e))

    def batch_preview(self):
        """Le plan du batch : une seule séquence de Sacs sur tous ses sets."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            self.output.delete("1.0", "end")
            self.status("Plan du batch…")
            wdb = get_wdb(self.status)
            p = batch.plan(name, wdb=wdb)
            st = p["stats"]
            self.log(f"=== Batch « {name} » ===")
            self.log(f"sets      : {', '.join(str(x) for x in st['multipliers'])}")
            self.log(f"×Quantité : {st['multipliers']}")
            self.log(f"{st['bags']} fichiers de sacs, {st['unique_parts']} lots uniques, "
                     f"{st['total_weight_g']/1000:.2f} kg")
            self.log(f"→ {st['sacs']} Sacs, une seule séquence 1..{st['sacs']}")
            self.log(f"consolidation : {st['consolidated_parts']} couleurs en C "
                     f"({st['cbins_used']} boîtes), "
                     f"{st['multi_colour_moulds']} moules multi-couleurs")
            if st.get("overweight_sacs"):
                self.log(f"⚠ Sacs au-dessus de la cible : {st['overweight_sacs']}")
            if st.get("mult_note"):
                self.log(st["mult_note"])
            # On ne compte que les Sacs qui ont du travail : ceux sans moule multi-couleurs
            # n'apparaissent pas non plus dans le fichier de consolidation.
            cp = [x for x in batch.consolidation_pass(p) if x["sous_sacs"]]
            self.log(f"\npasse de consolidation finale : "
                     f"{sum(len(x['sous_sacs']) for x in cp)} sous-sacs sur {len(cp)} Sacs "
                     f"(les autres n'ont aucun moule multi-couleurs)")
            self.status(f"Batch « {name} » : {st['sacs']} Sacs (aperçu, rien écrit).")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def batch_consolidation(self):
        """La liste des sous-sacs à confectionner, Sac par Sac — la passe de fin de batch."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            self.output.delete("1.0", "end")
            self.status("Liste de consolidation…")
            p = batch.plan(name, wdb=get_wdb(self.status), pin_sacs=True)
            cp = batch.consolidation_pass(p)
            res = batch.build_consolidation_bsx(name, wdb=get_wdb(self.status),
                                                log=lambda m: None)
            self.log(f"=== Consolidation du batch « {name} » ===")
            self.log(f"Fichier : {res['path']}")
            self.log("Ouvre-le dans BrickStore et trie sur la colonne Remarks : chaque Sac")
            self.log("défile avec, à la fin, les couleurs à réunir en sous-sac.\n")
            self.log("Un sous-sac par moule multi-couleurs. La passe est LOCALE : les")
            self.log("couleurs d'un moule sont déjà dans le même Sac, rien à chercher ailleurs.\n")
            for row in cp:
                if not row["sous_sacs"]:
                    continue
                self.log(f"— Sac {row['sac']:02d} ({row['lots']} lots, "
                         f"{len(row['sous_sacs'])} sous-sacs) —")
                for sub in row["sous_sacs"]:
                    self.log(f"   {sub['mould']:>12} : {', '.join(sub['colors'])}")
            self.status(f"{res['groups']} sous-sacs à confectionner — fichier écrit.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def batch_apply(self):
        """Écrit les remarques du batch dans les fichiers de tous ses sets."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            st = batch.status(name)
            if st["missing"]:
                messagebox.showerror("Batch", "Dossier introuvable : "
                                     + ", ".join(st["missing"]))
                return
            if not messagebox.askyesno(
                    "Appliquer le batch ?",
                    f"Écrire les remarques du batch « {name} » dans "
                    f"{len(st['sets'])} set(s) ?\n\n"
                    f"• {st['pieces']} pièces\n"
                    f"• une seule numérotation de Sacs sur tout l'envoi\n"
                    f"• chaque set garde son propre ×Quantité\n\n"
                    "Les sets sont vérifiés avant, et sauvegardés."):
                return
            self.output.delete("1.0", "end")
            self.status("Application du batch…")
            res = batch.apply(name, wdb=get_wdb(self.status),
                              backup=self.backup_var.get(), log=self.log)
            self.refresh()
            self.status(f"Batch « {name} » appliqué : {res['lots']} lots, "
                        f"{res['sacs']} Sacs.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def batch_cfb(self):
        """Un seul .bsx pour tout le batch, puis le courriel."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch, finalize
            st = batch.status(name)
            if st["missing"]:
                messagebox.showerror("Batch", "Dossier introuvable : "
                                     + ", ".join(st["missing"]))
                return
            if not messagebox.askyesno(
                    "Construire le CFB du batch ?",
                    f"Un seul fichier pour « {name} » "
                    f"({len(st['sets'])} sets, {st['pieces']} pièces) ?"):
                return
            self.output.delete("1.0", "end")
            self.status("Vérification avant envoi…")
            lines = []
            res = batch.build_cfb(name, log=lines.append)
            for l in lines:
                self.log(l)
            for w in res.get("warnings", []):
                self.log(w)
            self.log(f"=== Batch « {name} » ===")
            self.log(f"Maître : {res['master_lots']} lots, {res['master_qty']} pièces")
            self.log(f"  → {res['master_path']}")
            if res["restants_lots"]:
                self.log(f"Pièces « x » écartées : {res['restants_lots']} lots.")
            paths = [x for x in (res["master_path"], res["restants_path"]) if x]
            if config.CFB_APPS_SCRIPT_URL and messagebox.askyesno(
                    "Envoyer par courriel ?",
                    f"Envoyer {len(paths)} fichier(s) à {config.CFB_EMAIL_TO} ?"):
                reply = finalize.email_files(paths, subject=f"CFB — batch {name}")
                self.log(f"\nCourriel : {reply}")
                self.status(f"Batch envoyé ({reply}).")
                # Le batch est parti : on propose de le solder, comme le flux par set le
                # propose apres l'envoi d'un set. Par defaut NON.
                self.batch_finish(ask_only=True)
            else:
                self.status("Fichier du batch écrit.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def batch_finish(self, ask_only=False):
        """MARQUER LIVRÉ : l'envoi est parti, on le solde.

        C'est la dernière étape du cycle — « Prêt pour livraison » est posé tout seul quand le
        maître est construit, celle-ci se pose à la main quand le colis a réellement quitté la
        maison. Les copies sortent du stock (donc de l'inventaire non traité), les dossiers de
        sets vont à la corbeille, et les sets quittent la liste pour l'onglet **Historique**,
        qui les relit dans la table `sent`.

        Le dossier du batch reste, avec le maître et la consolidation — c'est la trace de ce
        qui a été expédié. `ask_only` sert juste après l'envoi du courriel, pour l'enchaîner
        sans redemander quel batch."""
        name = self._batch_name()
        if not name:
            return
        try:
            from sortpack import batch
            st = batch.status(name)
            lines = [f"Marquer le batch « {name} » comme LIVRÉ ?", "",
                     f"• {len(st['sets'])} set(s), {st['pieces']} pièces",
                     "• les copies sortent de l'inventaire (réversible)",
                     "• les dossiers de sets partent à la corbeille (récupérable)",
                     "• les sets passent dans l'onglet Historique",
                     "",
                     f"Le maître et la consolidation restent dans :",
                     f"   {batch.batch_dir(name)}"]
            # Marquer livré sans avoir construit le maître, c'est jeter les dossiers avant
            # d'avoir produit ce qu'on envoie. On n'interdit pas — on prévient.
            import glob as _glob
            if not _glob.glob(os.path.join(batch.batch_dir(name), f"{name}.bsx")):
                lines += ["", f"⚠ Aucun maître « {name}.bsx » dans le dossier du batch.",
                          "   « Construire le CFB du batch » n'a pas encore tourné."]
            if not messagebox.askyesno("Terminer le batch ?", "\n".join(lines),
                                       default=messagebox.NO, icon="warning"):
                if not ask_only:
                    self.status("Annulé — le batch reste ouvert.")
                return
            if not ask_only:
                self.output.delete("1.0", "end")
            self.log(f"\n=== Terminer le batch « {name} » ===")
            res = batch.finish(name, log=self.log)
            self.refresh()
            self.status(f"Batch « {name} » terminé : {res['removed']} copie(s) hors stock, "
                        f"{len(res['trashed'])} dossier(s) à la corbeille.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Batch", str(e))

    def open_folder(self):
        """Open the selected set's folder in the file explorer."""
        folder = self.selected_folder()
        if not folder:
            messagebox.showinfo("Choisir un set", "Sélectionne un set dans la liste d'abord.")
            return
        try:
            os.startfile(folder)   # Windows
        except AttributeError:
            import subprocess
            subprocess.Popen(["xdg-open", folder])   # non-Windows fallback
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible d'ouvrir le dossier :\n{e}")

    def _sid_for(self, name):
        """The Brickset image id for a set folder name: '11503 - …' → '11503-1'."""
        number = set_number_of(name)
        if not number:
            return None
        return number if "-" in number else f"{number}-1"

    def _row_name(self, iid, tree=None):
        """Le nom du set d'une ligne. Il vit dans la colonne « set » depuis que #0 porte la
        date d'achat (liste) ou la date de livraison (historique) — lire #0 rendrait une date."""
        tree = tree or self.tree
        try:
            return tree.set(iid, "set")
        except Exception:
            return ""

    def _load_row_images(self):
        """Kick off a one-time background download of each row's Brickset thumbnail; when it
        lands it's painted into that row's #0 cell. Cached (memory + temp file), so a refresh
        repaints instantly and nothing re-downloads."""
        rows = ([(iid, self.tree) for iid in self.tree.get_children()]
                + [(iid, self.htree) for iid in self.htree.get_children()])
        for iid, tree in rows:
            sid = self._sid_for(self._row_name(iid, tree))
            if not sid or sid in self._img_cache or sid in self._img_loading:
                continue
            self._img_loading.add(sid)
            import threading
            threading.Thread(target=self._fetch_thumb, args=(sid,), daemon=True).start()

    def _fetch_thumb(self, sid):
        img = None
        try:
            import urllib.request, tempfile, os as _os
            from PIL import Image
            cache_dir = _os.path.join(tempfile.gettempdir(), "mljq_setimg")
            _os.makedirs(cache_dir, exist_ok=True)
            fp = _os.path.join(cache_dir, sid + ".jpg")
            if not _os.path.exists(fp) or _os.path.getsize(fp) == 0:
                data = None
                for kind in ("small", "large"):     # small is a lighter thumbnail; large is a fallback
                    try:
                        url = f"https://images.brickset.com/sets/{kind}/{sid}.jpg"
                        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=15) as r:
                            data = r.read()
                        break
                    except Exception:
                        continue
                if not data:
                    raise RuntimeError("no image")
                with open(fp, "wb") as f:
                    f.write(data)
            img = Image.open(fp).convert("RGBA")
            img.load()
            img.thumbnail((self._thumb, self._thumb), Image.LANCZOS)
            pad = 10   # transparent right margin so the set name doesn't butt against the art
            canvas = Image.new("RGBA", (img.width + pad, img.height), (0, 0, 0, 0))
            canvas.paste(img, (0, 0))
            img = canvas
        except Exception:
            img = None
        # Hand the decoded PIL image back to the UI thread via a queue — NEVER touch Tk from
        # a worker thread (that call is silently dropped and the row never repaints).
        self._img_queue.put((sid, img))

    def _drain_img_queue(self):
        """Main-thread painter: turn queued PIL images into PhotoImages and drop them into
        their rows. Reschedules itself. (PhotoImage + tree edits must be on the UI thread.)"""
        try:
            from PIL import ImageTk
            while True:
                sid, img = self._img_queue.get_nowait()
                self._img_loading.discard(sid)
                if img is None:
                    continue
                try:
                    photo = ImageTk.PhotoImage(img)
                except Exception:
                    continue
                self._img_cache[sid] = photo
                for iid in self.tree.get_children():
                    if self._sid_for(self._row_name(iid)) == sid:
                        self.tree.item(iid, image=photo)
                for iid in self.htree.get_children():
                    if self._sid_for(self._row_name(iid, self.htree)) == sid:
                        self.htree.item(iid, image=photo)
        except queue.Empty:
            pass
        finally:
            self.root.after(200, self._drain_img_queue)

    def process_remote(self, manual=False):
        """Run the phone → PC queue (Appliquer restant / Construire CFB) AND the headless
        auto-apply pass (write remarks once a set is done cataloguing and BrickStore is
        closed). Off-thread so the window stays responsive; refreshes when done."""
        if self._remote_busy:
            if manual:
                self.status("Traitement déjà en cours…")
            return
        from sortpack import remote
        try:
            has = remote.has_work()
            n = remote.pending_count()
        except Exception:
            has, n = False, 0
        if not has:
            if manual:
                from sortpack import lock
                st = lock.status()
                if not (st["free"] or st["mine"] or st["stale"]):
                    # another machine holds the folder: say so, and let the user take it
                    if messagebox.askyesno(
                            "Verrou du dossier partagé",
                            f"{lock.describe()}\n\nPrendre le travail sur ce poste ?"):
                        lock.acquire(force=True, log=self.log)
                        self.status("Verrou pris — relance « Demandes ».")
                        return
                self.status("Rien à traiter (aucune demande, rien à appliquer).")
            return
        self._remote_busy = True
        self.status("Traitement des demandes / auto-application…")
        if manual:
            self.output.delete("1.0", "end")
            self.log(f"=== Demandes du téléphone ({n}) + auto-application ===")

        def tlog(m):
            self.output.after(0, lambda: self.log(m))

        def tstatus(m):
            self.output.after(0, lambda: self.status(m))

        def work():
            try:
                cmds = remote._load()["commands"]
                need_wdb = any(c.get("status") == "pending" and c.get("action") == "restants"
                               for c in cmds)
                if not need_wdb:
                    need_wdb = remote._has_autoapply_candidate()
                wdb = get_wdb(tstatus) if need_wdb else None
                done, errors = remote.process_pending(
                    log=tlog, backup=self.backup_var.get(), wdb=wdb, force=manual)
                applied = remote.auto_apply_ready(
                    log=tlog, wdb=wdb, backup=self.backup_var.get())

                def fin():
                    self.status(f"Traité : {done} demande(s), {applied} auto-appliqué(s), "
                                f"{errors} erreur(s).")
                    self.refresh()
                self.output.after(0, fin)
            except Exception as e:
                traceback.print_exc()
                self.output.after(0, lambda: (self.log(f"⚠ demandes : {e}"),
                                              self.status("Erreur — demandes du téléphone.")))
            finally:
                self._remote_busy = False

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _poll_remote(self):
        """Background heartbeat: process queued phone requests + auto-apply (config-gated),
        then reschedule. Cheap when idle — a tiny JSON read (+ a process check) decides."""
        try:
            _write_sort_index(build_index())   # keep the phone's set list + flags current
        except Exception:
            pass
        try:
            from sortpack import mobile         # phone's Achats / Finances tabs (throttled)
            mobile.maybe_write_dashboard()
        except Exception:
            pass
        try:
            if getattr(config, "REMOTE_AUTO_PROCESS", False) and not self._remote_busy:
                from sortpack import remote
                if remote.has_work():
                    self.process_remote(manual=False)
        except Exception:
            pass
        finally:
            self.root.after(int(getattr(config, "REMOTE_POLL_SECONDS", 25)) * 1000,
                            self._poll_remote)

    def _set_manual_status(self, name, new_status):
        """Pose à la main une phase que les fichiers ne peuvent pas deviner.

          config.STATUS_TRIER   → « je commence à trier » (passe par batch.request_trier)
          config.STATUS_SEPARER → « les sachets sont répartis » : pose `separated`, ce qui
                                  fait passer la pastille à « Cataloguer »
          "_reset"              → retour avant le tri : efface la phase ET les sacs cochés
        """
        if new_status == config.STATUS_TRIER:
            # « Je commence à trier » DÉCLENCHE l'application du batch — et se refuse tant que
            # l'envoi n'est pas fermé, parce que la numérotation des Sacs n'est pas figée.
            try:
                from sortpack import batch
                self.output.delete("1.0", "end")
                phase = batch.request_trier(_status_key(name), log=self.log)
                self.status(f"« {name} » → {phase}")
            except Exception as e:
                traceback.print_exc()
                messagebox.showerror("Trier", str(e))
            self.refresh(select_name=name)
            return
        state = _load_state()
        key = _status_key(name)
        entry = state.get(key) or {}
        if new_status == config.STATUS_SEPARER:
            # Séparation faite : rien d'autre ne bouge, la pastille passe à « Cataloguer ».
            entry["separated"] = datetime.datetime.now().isoformat(timespec="seconds")
        else:                                   # "_reset" : retour avant le tri
            entry.pop("status", None)
            entry.pop("bags_done", None)
        if entry:
            state[key] = entry
        else:
            state.pop(key, None)
        _save_state(state)
        self.refresh(select_name=name)

    def _show_set_menu(self, event):
        """Right-click menu on a set row: bump its status, track sorting, or open its folder."""
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        name = self._row_name(iid)
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="🗂 Suivi du tri…",
                         command=lambda: self.open_sort_tracker(name))
        menu.add_separator()
        menu.add_command(label="Marquer « sachets séparés »",
                         command=lambda: self._set_manual_status(name, config.STATUS_SEPARER))
        menu.add_command(label="Marquer « Trier »",
                         command=lambda: self._set_manual_status(name, config.STATUS_TRIER))
        menu.add_command(label="Remettre avant le tri",
                         command=lambda: self._set_manual_status(name, "_reset"))
        menu.add_separator()
        menu.add_command(label="📂 Ouvrir le dossier", command=self.open_folder)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_sort_tracker(self, name=None):
        """Per-bag sorting checklist for a set: one checkbox per numbered bag (1…N, where N
        is the biggest bag number). Ticking a bag records progress ('Trier 3/8') and the set
        flips to 'Trié ✓' once every bag is checked. Progress is saved to disk."""
        name = name or self.selected_name()
        if not name:
            messagebox.showinfo("Choisir un set", "Sélectionne un set dans la liste d'abord.")
            return
        folder = os.path.join(config.CATALOGUAGE_ROOT, name)
        total = bag_count(folder)
        if not total:
            messagebox.showinfo(
                "Suivi du tri",
                f"Aucun sac numéroté trouvé dans « {name} ».\n\nLes sacs apparaissent une "
                f"fois le cataloguage commencé (fichiers 1.bsx, 2.bsx, …).")
            return
        key = _status_key(name)

        win = tk.Toplevel(self.root)
        win.title(f"Suivi du tri — {name}")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=f"Coche chaque sac numéroté au fur et à mesure que tu le tries "
                            f"({total} sac(s)).", wraplength=360).pack(anchor="w")
        prog = ttk.Label(frm, text="", font=("Segoe UI", 10, "bold"))
        prog.pack(anchor="w", pady=(4, 8))

        done0 = set((_load_state().get(key) or {}).get("bags_done") or [])
        vars_by_bag = {}

        def save_progress():
            done = sorted(b for b, v in vars_by_bag.items() if v.get())
            state = _load_state()
            entry = state.get(key) or {}
            if done:
                entry["status"] = config.STATUS_TRIER   # ticking a bag = sorting started
                entry["bags_done"] = done
            else:
                entry.pop("bags_done", None)
            if entry:
                state[key] = entry
            else:
                state.pop(key, None)
            _save_state(state)
            n = len(done)
            prog.config(text=(f"✓ Tous les sacs triés ({n}/{total})" if n >= total
                              else f"{n} / {total} sac(s) trié(s)"),
                        foreground=("#137333" if n >= total else "#1f2328"))
            self.refresh(select_name=name)

        grid = ttk.Frame(frm)
        grid.pack(fill="both", expand=True)
        cols = 4 if total > 6 else 1
        for i, b in enumerate(range(1, total + 1)):
            v = tk.BooleanVar(value=(b in done0))
            vars_by_bag[b] = v
            ttk.Checkbutton(grid, text=f"Sac {b}", variable=v,
                            command=save_progress).grid(
                row=i // cols, column=i % cols, sticky="w", padx=6, pady=2)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(10, 0))

        def set_all(val):
            for v in vars_by_bag.values():
                v.set(val)
            save_progress()

        ttk.Button(btns, text="Tout cocher", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(btns, text="Tout décocher",
                   command=lambda: set_all(False)).pack(side="left", padx=6)
        ttk.Button(btns, text="Fermer", command=win.destroy).pack(side="right")

        save_progress()   # paint the initial count

    def _plan(self):
        folder = self.selected_folder()
        if not folder:
            messagebox.showinfo("Pick a set", "Select a set in the list first.")
            return None, None
        wdb = get_wdb(self.status)
        self.status("Building plan…")
        plan = build_plan(folder, wdb)
        return folder, plan

    def _show_plan(self, folder, plan):
        s = plan["stats"]
        self.output.delete("1.0", "end")
        self.log(f"=== {os.path.basename(folder)} ===")
        self.log(f"bags: {s['bags']}    unique parts: {s['unique_parts']}")
        self.log(f"consolidated: {s['consolidated_parts']}    C-bins: {s['cbins_used']}"
                 f"    Sacs: {s['sacs']}    total: {s['total_weight_g']/1000:.2f} kg")
        self.log(f"multi-colour moulds: {s.get('multi_colour_moulds', 0)}    "
                 f"colour-grouped parts: {s.get('color_grouped_parts', 0)}    "
                 f"D-bins: {s.get('bbins_used', 0)}")
        self.log(f"×Quantité (feuille) : {s['mult_status']}")
        if s["missing_weight"]:
            self.log(f"WARNING: {len(s['missing_weight'])} parts with no weight: "
                     f"{[k[0] for k in s['missing_weight'][:12]]}")
        self.log("")
        for label, path, items in plan["bags"]:
            rem = plan["remarks"].get(path, {})
            self.log(f"— bag {label} —")
            for it in items:
                if it["row"] in rem:
                    self.log(f"   {it['item_id']:>9}  {it['color_name'][:16]:16} "
                             f"x{it['qty']:<4} → {rem[it['row']]}")

    def preview(self):
        try:
            folder, plan = self._plan()
            if plan is None:
                return
            self._show_plan(folder, plan)
            self.status("Preview only — nothing written.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    def build_cfb(self):
        """Consolidate the selected set, split on the 'x' mark, write master + restants,
        then optionally email them via the Apps Script (if configured)."""
        try:
            folder = self.selected_folder()
            if not folder:
                messagebox.showinfo("Pick a set", "Select a set in the list first.")
                return

            # Guard: warn before overwriting existing CFB files for this set.
            existing = [p for p in finalize.planned_paths(folder) if os.path.exists(p)]
            if existing:
                names = "\n".join("• " + os.path.basename(p) for p in existing)
                if not messagebox.askyesno(
                        "Écraser ?",
                        f"Ces fichiers existent déjà dans BSX\\CFB :\n\n{names}\n\n"
                        f"Les écraser ?"):
                    self.status("Annulé (fichiers existants conservés).")
                    return

            from sortpack import batch as _batch
            b = _batch.batch_of(set_number_of(os.path.basename(folder)))
            if b:
                messagebox.showinfo(
                    "Ce set est dans un batch",
                    f"« {os.path.basename(folder)} » appartient au batch « {b} ».\n\n"
                    "Son maître est construit avec tout l'envoi : utilise "
                    "« Construire le CFB du batch ». Construire ce set seul donnerait "
                    "une liste partielle.")
                return
            self.status("Vérification avant envoi…")
            check_lines = []
            res = finalize.build_cfb_files(folder, log=check_lines.append)

            self.output.delete("1.0", "end")
            self.log(f"=== CFB : set {res['set_number']} ({res['bags']} sacs) ===")
            for line in check_lines:
                self.log(line)
            for w in res.get("warnings", []):
                self.log(w)
            inv = " + inventory" if res.get("included_inventory") else ""
            self.log(f"Master : {res['master_lots']} lots, {res['master_qty']} pièces{inv}")
            self.log(f"  → {res['master_path']}")
            # Meme regle que pour un batch : le maitre construit, le set est emballe et
            # attend le transporteur. Un set hors batch n'a pas a vivre un cycle different.
            try:
                from sortpack import batch as _b
                _b.mark_phase([res["set_number"]], config.STATUS_PRET, log=self.log)
                self.refresh(select_name=self.selected_name())
            except Exception as e:
                self.log(f"  ⚠ phase « {config.STATUS_PRET} » non posée : {str(e)[:70]}")
            if res["restants_lots"]:
                self.log(f"Pièces « x » écartées (non vendues) : {res['restants_lots']} "
                         f"lots, {res['restants_qty']} pièces.")
            else:
                self.log("Pièces « x » écartées : aucune.")

            paths = [p for p in (res["master_path"], res["restants_path"]) if p]
            if config.CFB_APPS_SCRIPT_URL:
                if messagebox.askyesno(
                        "Envoyer par courriel ?",
                        f"Envoyer {len(paths)} fichier(s) à {config.CFB_EMAIL_TO} ?"):
                    self.status("Envoi du courriel…")
                    reply = finalize.email_files(
                        paths, subject=f"CFB — set {res['set_number']}")
                    self.log(f"\nCourriel : {reply}")
                    self.status(f"Fichiers écrits et envoyés ({reply}).")
                else:
                    self.status("Fichiers écrits (envoi annulé).")
            else:
                self.log("\n(Apps Script non configuré — fichiers écrits, aucun envoi.)")
                self.status("Fichiers CFB écrits.")

            # Files are written — offer to mark the set processed: remove it from stock
            # AND send its now-done folder to the Recycle Bin (recoverable, never a hard
            # delete). The stock change writes a `sent` row so it survives the daily
            # reconcile; both run only on an explicit yes (default No).
            from sortpack import history, trash
            h = history.HistoryDB()
            try:
                on_hand = h.inventory_qty(res["set_number"]) or 0
                try:
                    nfiles = len(os.listdir(folder))
                except OSError:
                    nfiles = 0
                lines = [f"Marquer le set {res['set_number']} comme traité (envoyé au "
                         f"CFB) ?", ""]
                if on_hand > 0:
                    lines.append(f"• retirer {on_hand} copie(s) de l'inventaire")
                lines.append(f"• envoyer le dossier « {os.path.basename(folder)} » à la "
                             f"corbeille ({nfiles} fichier(s), récupérable)")
                if messagebox.askyesno("Traiter le set ?", "\n".join(lines),
                                       default=messagebox.NO, icon="warning"):
                    if on_hand > 0:
                        removed = h.send_to_cfb(res["set_number"])
                        self.log(f"\nInventaire : {removed} copie(s) retirée(s).")
                    try:
                        trash.send_to_recycle_bin(folder)
                        self.log(f"Dossier envoyé à la corbeille : "
                                 f"{os.path.basename(folder)}")
                        # Same cleanup as the phone path: the leftovers helper and the
                        # index / status / auto-apply rows outlive a folder that is gone.
                        from sortpack import remote as _remote
                        _remote._prune_phone_files(res["set_number"], log=self.log)
                        self.refresh()
                        self.status("Set traité (inventaire + dossier nettoyé).")
                    except Exception as e:
                        self.log(f"⚠ dossier non supprimé : {e}")
                        self.status("Set traité (dossier non supprimé — voir le journal).")
            finally:
                h.close()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    def sync_purchases(self):
        """Mirror the Journal's Catégorie-A purchases into the local DB (bought/inventory)
        so the ×Quantité multiplier is current — WITHOUT the full buy-report scraping.
        Reads the sheet, updates stock, and creates any missing CFB set folders."""
        try:
            self.output.delete("1.0", "end")
            self.status("Synchronisation des achats…")
            self.log("=== Synchroniser les achats (Journal → base) ===")
            from sortpack.catalogdb import CatalogDB
            from sortpack.history import HistoryDB
            from sortpack import purchases
            cat, hist = CatalogDB(), HistoryDB()
            try:
                res = purchases.sync(cat, hist, log=self.log)
            finally:
                hist.close()
            self.log(f"Transactions lues : {res.get('transactions', 0)}")
            self.log(f"Achats miroir dans la base : {res.get('journal', 0)}")
            if res.get("unmatched"):
                self.log(f"⚠ {res['unmatched']} ligne(s) ignorée(s) : numéro de set "
                         f"introuvable au catalogue (vérifie le numéro dans le Journal).")
            if res.get("new"):
                self.log(f"{res['new']} nouveau(x) set(s) — dossiers CFB créés.")
            self.log("\nLa ×Quantité est à jour. Relance l'Aperçu pour la voir.")
            _refresh_mobile("sync_purchases")
            self.status("Achats synchronisés.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    def log_purchase(self):
        """One-click purchase logging. Pick a set from the latest buy list (or type any set
        number — the name is confirmed against the catalog), enter the tax-included price
        paid, and a Catégorie-A row is posted to the Journal via the Apps Script:
        A='<vendeur> - <numéro> (<qté>)', B='A', C=−prix, D='CC', E=date. TPS/TVQ are the
        sheet's own formulas. The local DB is then synced (bought/inventory + CFB folder)."""
        import threading
        cat = get_catalog(self.status)

        # latest buy list → dropdown "numéro — nom", with vendor/price prefill
        options, meta = [], {}
        try:
            from sortpack.history import HistoryDB, BUY_SHEET_COLS
            i_sid, i_name = BUY_SHEET_COLS.index("set_id"), BUY_SHEET_COLS.index("name")
            i_src, i_price = BUY_SHEET_COLS.index("source"), BUY_SHEET_COLS.index("price_cad")
            h = HistoryDB()
            try:
                for row in h.latest_buy_list(chosen_only=True):   # the emailed batch
                    num = str(row[i_sid]).split("-")[0]
                    label = f"{num} — {row[i_name]}"
                    options.append(label)
                    meta[label] = {"vendor": (str(row[i_src] or "").capitalize() or "Amazon"),
                                   "price": row[i_price]}
            finally:
                h.close()
        except Exception:
            pass

        win = tk.Toplevel(self.root)
        win.title("J'ai acheté un set")
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Set (numéro, ou choisir dans la dernière liste d'achat) :")\
            .grid(row=0, column=0, columnspan=2, sticky="w")
        set_var = tk.StringVar()
        set_cb = ttk.Combobox(frm, textvariable=set_var, values=options, width=42)
        set_cb.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 2))
        name_lbl = ttk.Label(frm, text="", foreground="gray")
        name_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(frm, text="Vendeur :").grid(row=3, column=0, sticky="w")
        vendor_var = tk.StringVar(value="Amazon")
        ttk.Entry(frm, textvariable=vendor_var, width=20).grid(row=3, column=1, sticky="w")
        ttk.Label(frm, text="Quantité :").grid(row=4, column=0, sticky="w")

        def _qty_for(vendor):
            by = getattr(config, "BUY_QTY_BY_SOURCE", None) or {}
            return str(by.get((vendor or "").strip().lower(), config.TARGET_QTY_PER_SET))

        # La quantité suit le vendeur (×10 Costco, ×9 Amazon — la même règle que la liste
        # d'achat), sinon on proposerait 10 pour un achat Amazon qu'on a recommandé à 9.
        # Dès que la quantité est modifiée à la main, on cesse d'y toucher.
        qty_var = tk.StringVar(value=_qty_for(vendor_var.get()))
        _auto = {"last": qty_var.get()}

        def _sync_qty(*_a):
            if qty_var.get() != _auto["last"]:
                return                       # saisie manuelle : on respecte
            _auto["last"] = _qty_for(vendor_var.get())
            qty_var.set(_auto["last"])

        vendor_var.trace_add("write", _sync_qty)
        ttk.Entry(frm, textvariable=qty_var, width=8).grid(row=4, column=1, sticky="w")
        ttk.Label(frm, text="Prix TOTAL payé (taxes incl., pour toutes les copies) :")\
            .grid(row=5, column=0, sticky="w")
        price_var = tk.StringVar()
        ttk.Entry(frm, textvariable=price_var, width=12).grid(row=5, column=1, sticky="w")
        ttk.Label(frm, text="Date :").grid(row=6, column=0, sticky="w")
        date_var = tk.StringVar(value=datetime.date.today().isoformat())
        ttk.Entry(frm, textvariable=date_var, width=12).grid(row=6, column=1, sticky="w")

        def resolve_number(raw):
            return str(raw or "").split("—")[0].split("-")[0].strip()

        def on_set_change(*_):
            m = meta.get(set_var.get())
            if m:
                vendor_var.set(m["vendor"])
                if m.get("price"):                    # prefill the TOTAL estimate = unit × qty
                    try:
                        q = int(float(qty_var.get()))
                    except ValueError:
                        q = 1
                    price_var.set(f"{float(m['price']) * max(1, q):.2f}")
            num = resolve_number(set_var.get())
            if num and cat.has_set(num):
                name_lbl.config(text=f"✓ {cat.set_info(num).name}", foreground="green")
            elif num:
                name_lbl.config(text="numéro introuvable au catalogue", foreground="red")
            else:
                name_lbl.config(text="")
        set_var.trace_add("write", on_set_change)

        def save():
            num = resolve_number(set_var.get())
            if not num or not cat.has_set(num):
                messagebox.showerror("Set invalide", "Entre un numéro de set valide.")
                return
            try:
                qty = int(float(qty_var.get()))
                price = float(price_var.get().replace(",", ".").replace("$", "").strip())
            except ValueError:
                messagebox.showerror("Valeur invalide", "Quantité et prix doivent être numériques.")
                return
            if qty <= 0 or price <= 0:
                messagebox.showerror("Valeur invalide", "Quantité et prix doivent être > 0.")
                return
            vendor = vendor_var.get().strip() or "Amazon"
            date = date_var.get().strip() or datetime.date.today().isoformat()
            name = cat.set_info(num).name
            # H Lots / I Pièces du Journal, vérifiés contre les maîtres réellement expédiés
            # (43011, 42680, 77256 tombent au lot près et à la pièce près) :
            #   * les LOTS ne se multiplient PAS — acheter 9 exemplaires du même set donne les
            #     mêmes lots (pièce + couleur), avec 9× la quantité dedans ;
            #   * et ils se comptent SANS les pièces en trop, parce qu'une pièce en trop est
            #     presque toujours un doublon d'une pièce déjà au set : elle grossit un lot
            #     existant, elle n'en crée pas ;
            #   * les PIÈCES, elles, se multiplient, et comptent les extras — la boîte les
            #     contient physiquement et CFB les reçoit.
            tot_lots = len(cat.inventory(num, include_extras=False))
            tot_pieces = sum(p.qty for p in cat.inventory(num, include_extras=True)) * qty
            if not messagebox.askyesno(
                    "Confirmer l'achat",
                    f"Enregistrer au Journal :\n\n{vendor} - {num} ({qty})\n{name}\n\n"
                    f"Montant : −{price:.2f}$ (taxes incluses) · compte CC · {date}\n"
                    f"Lots : {tot_lots:,} · Pièces : {tot_pieces:,}\n\n"
                    f"Continuer ?"):
                return
            win.grab_release()
            win.destroy()
            self.output.delete("1.0", "end")
            self.status("Enregistrement de l'achat…")

            def work():
                try:
                    from sortpack import sheet, purchases
                    from sortpack.catalogdb import CatalogDB
                    from sortpack.history import HistoryDB
                    reply = sheet.post_purchase(vendor, num, qty, price, date=date,
                                                lots=tot_lots, pieces=tot_pieces)
                    self.output.after(0, lambda: self.log(
                        f"=== Achat enregistré au Journal ===\n{vendor} - {num} ({qty}) · "
                        f"−{price:.2f}$ · {tot_lots:,} lots · {tot_pieces:,} pièces · {date}\n"
                        f"Réponse : {reply}"))
                    cat2, hist = CatalogDB(), HistoryDB()
                    try:
                        res = purchases.sync(cat2, hist,
                                             log=lambda m: self.output.after(0, lambda: self.log(m)))
                    finally:
                        hist.close()
                    self.output.after(0, lambda: self.log(
                        f"\nSync : {res.get('journal', 0)} achat(s) miroir"
                        + (f", {res['new']} nouveau(x) — dossier CFB créé" if res.get("new") else "")
                        + "."))
                    self.output.after(0, lambda: self.status(
                        f"Achat enregistré : {vendor} - {num} ({qty})."))
                except Exception as e:
                    traceback.print_exc()
                    self.output.after(0, lambda: (messagebox.showerror("Erreur", str(e)),
                                                  self.status("Erreur — achat non enregistré.")))
            threading.Thread(target=work, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Enregistrer l'achat", command=save).pack(side="right")
        ttk.Button(btns, text="Annuler",
                   command=lambda: (win.grab_release(), win.destroy())).pack(side="right", padx=6)
        set_cb.focus_set()

    def compute_restants(self):
        """Point 3 — move the 'x'-marked lots into the set's Inventory file and write each
        leftover's Sac destination (reusing the Sacs already in progress). Backs up first."""
        try:
            folder = self.selected_folder()
            if not folder:
                messagebox.showinfo("Pick a set", "Select a set in the list first.")
                return
            from sortpack.plan import find_inventory
            inv = find_inventory(folder)
            if inv is None:
                messagebox.showerror(
                    "Pas d'inventaire",
                    "Aucun fichier « Inventory… » dans ce dossier de set.\n\nCe fichier "
                    "(pièces non cataloguées) doit exister pour calculer les restants.")
                return
            if not messagebox.askyesno(
                    "Calculer les restants ?",
                    f"Pour {os.path.basename(folder)} :\n\n"
                    f"• déplacer les lots marqués « x » des sacs vers\n"
                    f"  « {os.path.basename(inv)} »\n"
                    f"• y écrire la destination (Sac / C / B) de chaque restant\n\n"
                    f"Les fichiers touchés sont sauvegardés d'abord. Continuer ?"):
                self.status("Annulé.")
                return

            wdb = get_wdb(self.status)
            self.status("Calcul des restants…")
            res = restants.compute_restants(folder, wdb, backup=self.backup_var.get())

            self.output.delete("1.0", "end")
            self.log(f"=== Restants : set {res['set_number']} ===")
            self.log(f"Lots « x » déplacés : {res['moved_lots']} "
                     f"({res['moved_qty']} pièces) depuis {res['bags_touched']} sac(s).")
            self.log(f"Inventaire des restants : {res['inventory_lots']} lots, "
                     f"{res['inventory_qty']} pièces.")
            self.log(f"  → {res['inventory_path']}")
            if res["backup_dir"]:
                self.log(f"Sauvegarde : {res['backup_dir']}")
            self.log("\nOuvre le fichier Inventory, clique la colonne Remarques pour trier, "
                     "puis emballe. Supprime pour de bon les pièces toujours absentes.")
            self.status("Restants calculés.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    def inventory_cfb(self):
        """Read each seller's CFB inventory value (both portals, US→CAD), store the snapshot,
        and post it to this month's row: Binobrick → Résultats mensuels V, MLJQ → W (+
        Sommaire mensuel B/D). A seller not yet on the portal is skipped, not an error.
        Off-thread so the window stays responsive."""
        import threading
        from sortpack import cfb_inventory as cfb
        if not messagebox.askyesno(
                "Inventaire CFB",
                "Lire la valeur d'inventaire CFB (CA + US), l'enregistrer et l'inscrire "
                "au mois courant dans les feuilles ?"):
            return
        self.output.delete("1.0", "end")
        self.status("Lecture de l'inventaire CFB…")

        def work():
            try:
                cfb.capture(post=True, log=lambda m: self.output.after(0, lambda: self.log(m)))
                _refresh_mobile("cfb_inventory")
                self.output.after(0, lambda: self.status("Inventaire CFB enregistré et posté."))
            except Exception as e:
                traceback.print_exc()
                self.output.after(0, lambda: (messagebox.showerror("Erreur", str(e)),
                                              self.status("Erreur — inventaire CFB.")))

        threading.Thread(target=work, daemon=True).start()

    def new_cookies(self):
        """Modal form to paste fresh CFB session cookies (CA / US) from the browser. Saves
        them to the DB and checks them right away. A blank field keeps the current cookie."""
        import threading
        from sortpack import cfb_inventory as cfb

        win = tk.Toplevel(self.root)
        win.title("Nouveaux cookies CFB")
        win.geometry("660x380")
        win.transient(self.root)
        win.grab_set()
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left",
                  text="Colle la valeur complète de l'en-tête « Cookie » de chaque portail\n"
                       "(DevTools → Network → une requête → Request Headers → Cookie).\n"
                       "Laisse un champ vide pour garder le cookie actuel.").pack(anchor="w")

        ttk.Label(frm, text="CA — mocs.canadafirstbricks.com").pack(anchor="w", pady=(10, 0))
        ca_txt = tk.Text(frm, height=4, wrap="char")
        ca_txt.pack(fill="x")
        ttk.Label(frm, text="US — usmocs.canadafirstbricks.com").pack(anchor="w", pady=(8, 0))
        us_txt = tk.Text(frm, height=4, wrap="char")
        us_txt.pack(fill="x")

        status = ttk.Label(frm, text="", foreground="gray")
        status.pack(anchor="w", pady=(8, 0))

        def save():
            ca = ca_txt.get("1.0", "end").strip()
            us = us_txt.get("1.0", "end").strip()
            updated = []
            if ca:
                cfb.set_cookie("CA", ca)
                updated.append("CA")
            if us:
                cfb.set_cookie("US", us)
                updated.append("US")
            if not updated:
                status.config(text="Rien à enregistrer (les deux champs sont vides).",
                              foreground="gray")
                return
            status.config(text="Enregistré. Vérification en cours…", foreground="gray")

            def work():
                results = cfb.check_and_alert(sources=tuple(updated), email=False,
                                              log=lambda m: None)
                ok = all(r["ok"] for r in results)
                msg = " · ".join(f"{r['source']} : {'OK' if r['ok'] else 'échec'}"
                                 for r in results)
                def show():
                    if win.winfo_exists():
                        status.config(text=("✓ " if ok else "⚠ ") + msg,
                                      foreground=("#137333" if ok else "#c5221f"))
                    self.log(f"Cookies mis à jour : {', '.join(updated)} — {msg}")
                    _refresh_mobile("check_cookies")
                    self.status("Cookies valides." if ok else "Un cookie ne fonctionne pas.")
                self.output.after(0, show)

            threading.Thread(target=work, daemon=True).start()

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Enregistrer et vérifier", command=save).pack(side="right")
        ttk.Button(row, text="Fermer", command=win.destroy).pack(side="right", padx=6)
        ca_txt.focus_set()

    def end_of_month(self):
        """'I'm ready for end-of-month': re-read the financial sheet and lock in each
        month's investable budget (prior month's net-worth Delta × MLJQ rate), then show
        the per-month budgets and what's available now. Off-thread; the sheet formula
        computes the Delta itself once you enter the month's Fortune."""
        import threading
        from sortpack import job, config
        from sortpack.history import HistoryDB
        self.output.delete("1.0", "end")
        self.status("Calcul du budget du mois…")

        def work():
            h = HistoryDB()
            rate, stored = None, []
            try:
                try:
                    # post=True : le but du geste est que le « Montant à investir » apparaisse
                    # dans le Sommaire mensuel. Le calculer sans l'écrire ne sert à personne.
                    rate, stored = job.refresh_budget(h, post=True)
                except Exception as e:
                    msg = f"⚠ Feuille financière injoignable ({str(e)[:80]}) — budget fixe."
                    self.output.after(0, lambda: self.log(msg))
                avail, _ = h.available_budget(config.MONTHLY_BUDGET_CAD,
                                              config.BUDGET_ANCHOR_MONTH)
                spent = h.spent_since(config.BUDGET_ANCHOR_MONTH)
            finally:
                h.close()

            def show():
                self.log(f"=== Budget mensuel (taux MLJQ {rate:.0%}) ===" if rate
                         else "=== Budget mensuel ===")
                if stored:
                    for month, amount in stored:
                        self.log(f"  {month} : {amount:,.2f}$")
                else:
                    self.log(f"  (aucun budget dynamique calculable — repli fixe "
                             f"{config.MONTHLY_BUDGET_CAD:,.0f}$)")
                self.log("")
                self.log(f"Disponible aujourd'hui (depuis l'ancre "
                         f"{config.BUDGET_ANCHOR_MONTH}) : {avail:,.2f}$")
                self.log(f"  (déjà dépensé {spent:,.2f}$)")
                _refresh_mobile("end_of_month")
                self.status(f"Budget prêt — {avail:,.0f}$ disponible.")
            self.output.after(0, show)

        def runner():
            try:
                work()
            except Exception as e:
                self.output.after(0, lambda: (traceback.print_exc(),
                                              messagebox.showerror("Erreur", str(e)),
                                              self.status("Erreur pendant le budget.")))

        threading.Thread(target=runner, daemon=True).start()

    def run_buy_report(self, post=False):
        """Manually trigger the headless buy-report job (scrape Amazon → history →
        rank → CSV → optional sheet). Runs off-thread so the window stays responsive."""
        import threading
        from sortpack import job

        where = " et publier au Google Sheet (écrase le rapport précédent)" if post else ""
        if not messagebox.askyesno(
                "Rapport d'achat",
                f"Rafraîchir les rabais Amazon + Costco, recalculer le lot du mois{where} ?\n"
                "(entièrement headless — aucune fenêtre de navigateur)"):
            return
        self.output.delete("1.0", "end")
        self.status("Rapport d'achat en cours…")

        def log(msg):
            self.output.after(0, lambda: self.log(msg))

        def done(s):
            self.log("")
            self.log(f"✔ {s['date']} : {s['amazon']} Amazon + {s['costco']} Costco · "
                     f"{s['candidates']} candidats · lot {s['chosen']} sets "
                     f"(enregistré dans l'historique)")
            self.log(f"   coût {s['batch_cost']:.0f}$  ·  profit/an "
                     f"{s['batch_annual_profit']:.0f}$")
            if s["posted"]:
                self.log("   → envoyé au Google Sheet.")
            _refresh_mobile("buy_report")
            self.status(f"Rapport prêt — lot de {s['chosen']} sets "
                        f"(profit/an {s['batch_annual_profit']:.0f}$).")

        def fail(e):
            traceback.print_exc()
            messagebox.showerror("Erreur", str(e))
            self.status("Erreur pendant le rapport d'achat.")

        def work():
            try:
                s = job.run(post=post, log=log)
                self.output.after(0, lambda: done(s))
            except Exception as e:
                self.output.after(0, lambda: fail(e))

        threading.Thread(target=work, daemon=True).start()

    def verify_set(self):
        """Cross-check the selected set's files against its real inventory and offer to put
        the difference back. Runs on demand — handy DURING cataloguing, since an inventory
        left too full is exactly what keeps a set from ever looking finished."""
        folder = self.selected_folder()
        if not folder:
            messagebox.showinfo("Choisis un set", "Sélectionne d'abord un set dans la liste.")
            return
        try:
            self.output.delete("1.0", "end")
            self.status("Vérification des quantités…")
            cat = get_catalog(self.status)
            rep = verify.check(folder, cat)
            verify.log_report(rep, self.log)
            if not rep["diffs"]:
                self.status("Quantités conformes au catalogue.")
                return
            if not rep["can_fix"]:
                messagebox.showwarning("Vérification", rep["blocked"])
                self.status("Écarts détectés — non corrigés.")
                return
            if not messagebox.askyesno(
                    "Corriger les quantités ?",
                    f"{len(rep['diffs'])} lot(s) en écart ({rep['gap_qty']} pièce(s)).\n\n"
                    f"Corriger les fichiers maintenant ?\n"
                    f"(les manquants retournent à l'inventaire, le surplus en est retiré)"):
                self.status("Vérification seulement — rien n'a été écrit.")
                return
            rep = verify.reconcile(folder, cat, fix=True, log=self.log,
                                   backup=self.backup_var.get())
            verify.log_report(rep, self.log)
            self.status(f"Quantités corrigées — {len(rep['diffs'])} lot(s).")
            self.refresh()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    def value_set(self):
        """Part-out value of a set, read straight from BrickStore's database
        (no BrickStore running, no XML). Uses the selected folder's set number as
        the default, otherwise asks."""
        try:
            default = ""
            name = self.selected_name()
            if name:
                default = set_number_of(name) or ""
            set_no = simpledialog.askstring(
                "Valeur du set",
                "Numéro de set BrickLink (ex. 76342) :",
                initialvalue=default, parent=self.output.winfo_toplevel())
            if not set_no:
                return
            set_no = set_no.strip()

            cat = get_catalog(self.status)
            pg = get_pg(self.status)
            if not cat.has_set(set_no):
                messagebox.showinfo(
                    "Introuvable",
                    f"Le set {set_no} n'est pas dans le catalogue BrickStore.\n"
                    f"(Fais « Update Database » dans BrickStore si c'est un set récent.)")
                self.status("Set introuvable.")
                return

            self.status("Calcul de la valeur…")
            v = setvalue.set_value(cat, pg, set_no)  # extras excluded
            self._show_value_window(v, cat, pg, set_no)
            net = v.recovery_value * config.USD_TO_CAD * (1 - config.CFB_FEE)
            self.status(
                f"{v.set_info.set_id} — récupération nette ≈ {net:,.0f} $CA · "
                f"prix d'achat max (ROI {config.ROI_FLOOR:g}) ≈ {net/config.ROI_FLOOR:,.0f} $CA")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")

    # ---------------------------------------------------------------- value UI
    def _show_value_window(self, v, cat, pg, set_no):
        """A compact 'tableau' popup: the two numbers that drive a buy decision
        (net recovery + max buy price), every part-out metric, liquidity/momentum,
        and the top parts. All read live from BrickStore's DB."""
        cfg = config
        si = v.set_info
        basis = cfg.RECOVERY_BASIS
        net_cad = v.recovery_value * cfg.USD_TO_CAD * (1 - cfg.CFB_FEE)
        max_buy = net_cad / cfg.ROI_FLOOR if cfg.ROI_FLOOR else 0.0

        # aggregate parts: weighted sell-ratio + top contributors
        parts = cat.inventory(set_no)
        contribs = []
        for p in parts:
            rec = pg.get(p.price_key())
            if not rec:
                continue
            contribs.append((p.qty * rec.current_new_avg, p, rec))
        contribs.sort(reverse=True, key=lambda x: x[0])
        months = setvalue.months_to_liquidate(v.weighted_ratio)
        six = v.totals["sixmonth_new_avg"]
        momentum = (v.totals["current_new_avg"] / six - 1) if six else 0.0

        # palette (light; readable on Windows default)
        BG, CARD, INK, MUTE = "#ffffff", "#f6f8fa", "#1f2328", "#656d76"
        GREEN, BLUE, LINE = "#1a7f37", "#0969da", "#d0d7de"

        win = tk.Toplevel(self.output.winfo_toplevel())
        win.title(f"Valeur — {si.set_id}")
        win.configure(bg=BG)
        win.geometry("710x840")

        st = ttk.Style(win)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("Val.Treeview", background=BG, fieldbackground=BG,
                     foreground=INK, rowheight=23, borderwidth=0)
        st.configure("Val.Treeview.Heading", font=("Segoe UI", 9, "bold"),
                     foreground=MUTE)
        st.map("Val.Treeview", background=[("selected", "#ddf4ff")],
               foreground=[("selected", INK)])

        f_head = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        f_sub = tkfont.Font(family="Segoe UI", size=10)
        f_big = tkfont.Font(family="Segoe UI", size=22, weight="bold")
        f_lbl = tkfont.Font(family="Segoe UI", size=9)
        f_sec = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        f_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        PAD = dict(padx=16)

        def section(text):
            tk.Label(win, text=text, font=f_sec, bg=BG, fg=INK,
                     anchor="w").pack(fill="x", pady=(12, 2), **PAD)

        # -- header --
        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", pady=(14, 2), **PAD)
        tk.Label(head, text=si.name, font=f_head, bg=BG, fg=INK,
                 anchor="w").pack(fill="x")
        yr = f" · {si.year_from}" if si.year_from else ""
        tk.Label(head, text=f"{si.set_id}{yr}   ·   {v.lots} lots   ·   "
                 f"{v.total_qty} pièces   ·   couverture {v.coverage:.0%}",
                 font=f_sub, bg=BG, fg=MUTE, anchor="w").pack(fill="x")

        # -- hero cards: the two decision numbers --
        hero = tk.Frame(win, bg=BG)
        hero.pack(fill="x", pady=10, **PAD)

        def card(title, value, color, side_pad):
            c = tk.Frame(hero, bg=CARD, highlightbackground=LINE,
                         highlightthickness=1)
            tk.Label(c, text=title, font=f_lbl, bg=CARD, fg=MUTE,
                     anchor="w").pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(c, text=value, font=f_big, bg=CARD, fg=color,
                     anchor="w").pack(anchor="w", padx=12, pady=(0, 10))
            c.pack(side="left", expand=True, fill="both", padx=side_pad)

        card("RÉCUPÉRATION NETTE", f"${net_cad:,.0f}", GREEN, (0, 6))
        card("PRIX D'ACHAT MAX", f"${max_buy:,.0f}", BLUE, (6, 0))
        tk.Label(win, text=f"prix du {datetime.date.fromtimestamp(v.price_mtime)}",
                 font=f_lbl, bg=BG, fg=MUTE, anchor="w").pack(fill="x", **PAD)

        # -- all part-out metrics (CAD) --
        section("PART-OUT (CAD)")
        mf = tk.Frame(win, bg=BG)
        mf.pack(fill="x", **PAD)
        tv = ttk.Treeview(mf, columns=("cad",), show="tree headings",
                          style="Val.Treeview", height=len(METRIC_ORDER))
        tv.heading("#0", text="Métrique")
        tv.heading("cad", text="CAD")
        tv.column("#0", width=380, anchor="w")
        tv.column("cad", width=140, anchor="e")
        for i, m in enumerate(METRIC_ORDER):
            tv.insert("", "end", text="  " + METRIC_LABELS[m],
                      values=(f"${v.totals[m]*cfg.USD_TO_CAD:,.2f}",),
                      tags=("odd" if i % 2 else "even",))
        tv.tag_configure("odd", background=CARD)
        tv.pack(fill="x")

        # -- vitesse --
        section("VITESSE")
        sig = tk.Frame(win, bg=BG)
        sig.pack(fill="x", **PAD)

        def sigrow(label, value, color=INK):
            r = tk.Frame(sig, bg=BG)
            r.pack(fill="x")
            tk.Label(r, text=label, font=f_lbl, bg=BG, fg=MUTE, anchor="w",
                     width=28).pack(side="left")
            tk.Label(r, text=value, font=f_bold, bg=BG, fg=color).pack(side="left")

        sigrow("Ratio de vente", f"{v.weighted_ratio:.2f}")
        sigrow("Délai de liquidation", f"{months:.0f} mois",
               GREEN if months <= 12 else ("#cf222e" if months >= 36 else INK))
        sigrow("Momentum", f"{momentum*100:+.1f}%",
               GREEN if momentum >= 0 else "#cf222e")

        # -- top parts --
        section("TOP PIÈCES")
        pf = tk.Frame(win, bg=BG)
        pf.pack(fill="both", expand=True, **PAD)
        pv = ttk.Treeview(pf, columns=("col", "qte", "val", "ratio"),
                          show="tree headings", style="Val.Treeview", height=8)
        pv.heading("#0", text="Pièce")
        pv.heading("col", text="Couleur")
        pv.heading("qte", text="Qté")
        pv.heading("val", text="Valeur CAD")
        pv.heading("ratio", text="Ratio")
        pv.column("#0", width=110, anchor="w")
        pv.column("col", width=200, anchor="w")
        pv.column("qte", width=60, anchor="e")
        pv.column("val", width=110, anchor="e")
        pv.column("ratio", width=70, anchor="e")
        for i, (val, p, rec) in enumerate(contribs[:8]):
            pv.insert("", "end", text=f"  {p.item_type}{p.item_id}",
                      values=(p.color_name, p.qty,
                              f"${val*cfg.USD_TO_CAD:,.2f}",
                              f"{rec.ratio_new or 0:.1f}"),
                      tags=("odd" if i % 2 else "even",))
        pv.tag_configure("odd", background=CARD)
        pv.pack(fill="both", expand=True)

        tk.Button(win, text="Fermer", command=win.destroy).pack(pady=10)
        win.transient(self.output.winfo_toplevel())

    def apply(self):
        try:
            folder, plan = self._plan()
            if plan is None:
                return
            self._show_plan(folder, plan)
            n = plan["stats"]["unique_parts"]
            if not messagebox.askyesno(
                    "Write remarks?",
                    f"Write remarks into the bag files of\n\n{os.path.basename(folder)}\n\n"
                    f"({plan['stats']['bags']} bags, {n} parts)?"):
                self.status("Cancelled.")
                return

            # Guard: the set has no Quantité in the sheet (or it couldn't be read).
            # Applying now would leave quantities un-multiplied — warn before writing.
            if config.CFB_MULTIPLY_FROM_SHEET and not plan["stats"]["mult_ok"]:
                if plan["stats"].get("mult_error") == "not_found":
                    m = (f"Le set {os.path.basename(folder)} n'a pas de Quantité dans la "
                         f"feuille Inventaire.\n\nSi tu appliques maintenant, AUCUNE "
                         f"multiplication ne sera faite (×1) et les remarks seront basées "
                         f"sur les quantités de base.\n\nRecommandé : Annuler, ajouter la "
                         f"ligne dans la feuille, puis relancer.\n\nAppliquer quand même à ×1 ?")
                else:
                    m = (f"La Quantité n'a pas pu être lue depuis la feuille :\n\n"
                         f"{plan['stats']['mult_note']}\n\nAppliquer quand même à ×1 ?")
                if not messagebox.askyesno("Quantité manquante", m, icon="warning"):
                    self.status("Annulé — Quantité manquante dans la feuille.")
                    return

            # Cross-check the hand-cataloguing against the set's real inventory and put
            # the difference back BEFORE planning: fixing can add or drop lots, and the
            # plan addresses its remarks by row.
            rep = verify.before_apply(folder, cat=get_catalog(self.status), log=self.log,
                                      backup=self.backup_var.get())
            if rep and rep.get("fixed"):
                self.log("  (quantités corrigées — plan recalculé)")
                plan = build_plan(folder, get_wdb(self.status))

            total, backup_dir = apply_plan(
                folder, plan, backup=self.backup_var.get())
            if backup_dir:
                self.log(f"\nBacked up originals to:\n  {backup_dir}")
            self.log(f"\n✔ Wrote remarks into {total} lots.")
            self.status(f"Done — {total} lots written.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
            self.status("Error.")


def _set_app_id():
    """Declare a stable Windows AppUserModelID BEFORE any window exists. Without it, a
    pythonw-hosted app is grouped on the taskbar under pythonw.exe and shows the generic
    Python icon; with it, Windows uses the window's own icon (set below) for the taskbar
    button. Best-effort; ignored off Windows."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MLJQ.SortPackHelper")
    except Exception:
        pass


def _apply_window_icon(root):
    """Give the window (title bar + taskbar button) the app icon. We set it TWO ways because
    Tk's iconbitmap can silently fail on some .ico files: iconbitmap for the .ico, plus
    iconphoto from the PNG (which also drives the taskbar). Best-effort throughout."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    ico, png = os.path.join(base, "mljq.ico"), os.path.join(base, "mljq.png")
    try:
        if os.path.exists(ico):
            root.iconbitmap(default=ico)
    except Exception:
        pass
    try:
        if os.path.exists(png):
            img = tk.PhotoImage(file=png)   # Tk 8.6 reads PNG natively
            root.iconphoto(True, img)
            root._icon_ref = img            # keep a reference so Tk doesn't GC it
    except Exception:
        pass


def main():
    _set_app_id()
    root = tk.Tk()
    _apply_window_icon(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
