# -*- coding: utf-8 -*-
"""
All tunable business rules live here. Edit these, not the algorithm code.
"""

import os

# Project root (this file is sortpack/config.py).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Buy-report economics (knobs for the value / "what to buy" logic) ---------
# These turn a raw part-out value into "what I actually get back" and eventually
# feed the buy ranking. Tune here.
USD_TO_CAD = 1.37
# Cut taken when the parted-out lots are sold (Canada First Brick). Net recovery
# keeps (1 - CFB_FEE).
CFB_FEE = 0.25
# A GST/HST-registered company recovers the sales tax it pays on purchases (input
# tax credits), so tax on the purchase price is a wash — leave it out of "cost".
INCLUDE_PURCHASE_TAX = False
PURCHASE_TAX_RATE = 0.14975     # QC GST+QST, only applied if INCLUDE_PURCHASE_TAX
# Which price drives recovery: actual 6-month SOLD prices are realistic; current
# "asking" listings run ~20-25% hot. One of priceguide.METRIC_ORDER.
RECOVERY_BASIS = "sixmonth_new_avg"
# HOW MUCH you recover (the amount): over the ~5-year horizon almost every set realizes
# roughly the same fraction of its theoretical part-out value — dregs, price erosion —
# so this is a FLAT rate on the whole set, NOT a per-part liquidity discount. Before the
# CFB fee. net_recovery = part_out(basis) * REALIZATION_RATE * (1 - CFB_FEE).
REALIZATION_RATE = 0.70
# HOW FAST you get there (the real differentiator): months to liquidate a set is modeled
# as SELL_WINDOW_MONTHS / weighted_sell_ratio (ratio = sold-6mo / listed), clamped to
# [MIN, MAX]. Faster sets get a higher ANNUALIZED ROI = roi ** (12 / months), which is
# what the buy list ranks by — capital that comes back sooner beats a fat slow return.
SELL_WINDOW_MONTHS = 6.0     # the price guide's sales window (the ratio's denominator)
MIN_SELL_MONTHS = 2.0
MAX_SELL_MONTHS = 60.0       # ~5 years: the full-realization horizon
# Monthly buying budget (CAD) and batch size, plus a flex: a standout set may push
# the batch over budget by up to this fraction when its ROI clearly justifies it.
# FALLBACK only: the real budget is now computed per month from the financial sheet
# (net-worth Delta of the prior month × MLJQ invest rate) and stored in the DB
# (monthly_budget); this flat value is used only if that sheet is unreachable.
MONTHLY_BUDGET_CAD = 2250.0
BATCH_SIZE = 10             # max distinct sets in a batch (budget usually binds first)
BUDGET_FLEX = 0.35          # allow up to +35% over budget for an exceptional set
# We buy this many copies of each chosen set (availability-permitting; the real qty is
# reconciled from the Journal). Lot cost = qty x unit price; returns are shown x qty.
TARGET_QTY_PER_SET = 10
# ... mais le nombre d'exemplaires depend en fait du magasin : Costco vend par palette, on en
# prend 10 ; Amazon est plus contraint, 9. Le compte de PIECES d'un achat (pieces du set x
# exemplaires) est ce qui remplit un batch, d'ou l'importance de ne pas melanger les deux.
# Une source inconnue retombe sur TARGET_QTY_PER_SET.
BUY_QTY_BY_SOURCE = {"costco": 10, "amazon": 9}

# Le plancher d'un envoi, en pieces : CFB range son inventaire par passes et demande des lots
# de cette taille. C'est la cible que l'achat vise (en deduisant ce qu'un batch deja entame
# contient deja) et celle que batch.status compare.
BATCH_TARGET_PIECES = 20000
# Budget accrues MONTHLY_BUDGET_CAD each month from this anchor (yyyy-mm). Available =
# monthly x months_elapsed - already-spent-since-anchor, so unspent months roll forward
# (rattrapage). Spend is measured PRE-TAX (Journal Montant minus TPS+TVQ; tax is an ITC).
BUDGET_ANCHOR_MONTH = "2026-09"
# Safety gate on the plain (non-annualized) ROI: net recovery must be >= this x cost —
# you must make money on the trade before speed even matters.
ROI_FLOOR = 1.2
# Don't re-buy a set we already bought within this many months (avoids flooding the
# market with parts we're still selling). Purchases come from the Journal's Catégorie-A
# transactions; see sortpack/purchases.py.
REBUY_WINDOW_MONTHS = 18
# Price-erosion realization. The part-out uses the 6-month SOLD average, but a rare/
# unpopular new part (a scarce new mould or colour) is often already listed BELOW what it
# last sold for — its price is eroding and won't hold. Using only real data (recent-sold
# vs currently-listed), we value each such part at the lower current price. Stable/liquid
# parts (listed >= sold) keep full value. No invented parameters, quantity-independent.
REALIZATION_ADJUST = True
# Predictive thin-demand haircut (an ASSUMPTION, layered on the real erosion signal above):
# a part that barely sells — few actual sales in 6 months — has soft demand, so its high
# scarcity price won't hold and will drift down further. We discount it by how far its
# 6-month sales fall below DEMAND_HEALTHY (units considered "moving"). This uses ABSOLUTE
# sales, not the sold/listed ratio, because a huge common part (thousands listed, hundreds
# sold) has a low ratio yet sells fine. VELOCITY_STRENGTH 0 = off (pure erosion only);
# the haircut is capped so a part is never cut below VELOCITY_FLOOR from thinness alone.
DEMAND_HEALTHY = 6
VELOCITY_STRENGTH = 0.4
VELOCITY_FLOOR = 0.4
# Warn (email banner) if BrickStore's catalog (database-v12) hasn't been refreshed in
# this many days — new sets only appear after you run BrickStore "Update Database".
CATALOG_STALE_DAYS = 14
# Warn if BrickStore's price cache (priceguide_cache.sqlite) is older than this — the
# valuations are only as fresh as the last "Update Price Guide". (Its mtime bumps on any
# BrickStore price fetch, so this is a floor: it fires only if you haven't touched prices
# at all in that window.)
PRICEGUIDE_STALE_DAYS = 30
# A set only clears the safety floor as a candidate if we can price this fraction of
# its lots (avoids garbage from sets with lots of price-less parts).
MIN_COVERAGE = 0.90
# The flex band (budget .. budget*(1+flex)) is reserved for "just that good" sets:
# only a set with ROI >= STANDOUT_ROI (absolute) may push the batch over budget.
STANDOUT_ROI = 2.4

# The Amazon discount feed is scraped headlessly from Brickset (see sortpack/amazon.py)
# and stored in the history DB's amazon_price table — no CSV. The buy list ranks the
# most recent scrape (HistoryDB.amazon_latest) and is itself stored in the buy_list
# table (HistoryDB.snapshot_buy_list). The SQLite is the single source of truth.

# Optional: POST the ranked buy list to a Google Sheet via an Apps Script Web App
# doPost (see appscript/CFB_email.gs). Leave empty to disable (the job still writes the
# CSV + history). Deploy the Apps Script, paste its /exec URL here, and use the same
# shared token as CFB_APPS_SCRIPT_TOKEN.
BUY_REPORT_POST_URL = "https://script.google.com/macros/s/AKfycbzzmFzyiDScqG5vW05Mi3G5m_lD5ec9afGQJpq68yvNWLmLJFhQ3lAnNpEGRljj0ixN/exec"
BUY_REPORT_SHEET_TAB = "Achats"     # sheet tab the buy list overwrites on each POST

# Recipients of the "Lot du mois" buy-report email (comma-separated string OR list).
# Separate from CFB_EMAIL_TO so the buy list can go to more people than the CFB-file emails.
BUY_REPORT_EMAIL_TO = "jeremie.queenton@gmail.com, marjolaine.lapierre12@gmail.com"

# The daily job also posts the current "reste à investir" — the cumulative available budget
# (Σ monthly budgets since anchor − spent, rattrapage-aware) — to ONE cell of the accounting
# spreadsheet's dashboard tab, overwritten each run (a single running figure, not per-month).
DASHBOARD_TAB = "Dashboard"
DASHBOARD_CELL = "G3"

# The business knobs above can be overridden at runtime from the Google Sheet's
# "Paramètres" tab (keys prefixed "mljq_", col A=key, col B=value), so they're tunable
# without editing code. apply_sheet_params() maps sheet keys -> these module attrs.
def _as_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "vrai", "oui")

# sheet key (mljq_*) -> (config attribute, converter)
_SHEET_KEY_MAP = {
    "mljq_usd_to_cad": ("USD_TO_CAD", float),
    "mljq_cfb_fee": ("CFB_FEE", float),
    "mljq_realization_rate": ("REALIZATION_RATE", float),
    "mljq_recovery_basis": ("RECOVERY_BASIS", str),
    "mljq_roi_floor": ("ROI_FLOOR", float),
    "mljq_monthly_budget_cad": ("MONTHLY_BUDGET_CAD", float),
    "mljq_batch_size": ("BATCH_SIZE", int),
    "mljq_budget_flex": ("BUDGET_FLEX", float),
    "mljq_min_coverage": ("MIN_COVERAGE", float),
    "mljq_rebuy_window_months": ("REBUY_WINDOW_MONTHS", int),
    "mljq_sell_window_months": ("SELL_WINDOW_MONTHS", float),
    "mljq_min_sell_months": ("MIN_SELL_MONTHS", float),
    "mljq_max_sell_months": ("MAX_SELL_MONTHS", float),
    "mljq_standout_roi": ("STANDOUT_ROI", float),
    "mljq_include_purchase_tax": ("INCLUDE_PURCHASE_TAX", _as_bool),
    "mljq_purchase_tax_rate": ("PURCHASE_TAX_RATE", float),
    "mljq_target_qty_per_set": ("TARGET_QTY_PER_SET", int),
    "mljq_budget_anchor_month": ("BUDGET_ANCHOR_MONTH", str),
    "mljq_catalog_stale_days": ("CATALOG_STALE_DAYS", int),
    "mljq_priceguide_stale_days": ("PRICEGUIDE_STALE_DAYS", int),
    "mljq_demand_healthy": ("DEMAND_HEALTHY", int),
    "mljq_velocity_strength": ("VELOCITY_STRENGTH", float),
}


def apply_sheet_params(params):
    """Override the business knobs from a {key: value} dict (the Paramètres tab).
    Unknown keys and bad values are ignored (defaults above stay). Returns the list of
    attributes actually changed."""
    changed = []
    g = globals()
    for key, (attr, conv) in _SHEET_KEY_MAP.items():
        if key not in params or str(params[key]).strip() == "":
            continue
        raw = params[key]
        try:
            if conv in (float, int):
                # tolerate fr-locale commas ("1,2"); a date-mangled cell (e.g. Sheets
                # turned "1.2" into 2026-01-02) fails float() → keeps the code default.
                num = float(str(raw).replace(",", ".").strip())
                g[attr] = int(num) if conv is int else num
            else:
                g[attr] = conv(raw)
            changed.append(attr)
        except (TypeError, ValueError):
            pass
    return changed


# --- CFB inventory value tracker ---------------------------------------------
# Which seller's batch inventory report to read from the CFB/UFB portals. The report
# form has a vendor dropdown; we pick the option whose label contains this (case-
# insensitive). The MLJQ seller is now live on both portals, so this is the real
# operating vendor for the sales flow. Pass --vendor NAME on the CLI to override (e.g.
# --vendor Bino to smoke-test against Binobrick).
CFB_VENDOR = "MLJQ"

# One canonical seller can carry a slightly different label on each portal. The US portal
# (usmocs) currently lists the MLJQ seller as "MLJC" (a typo Sylvain is fixing); the CA
# portal lists it as "MLJQ". Map the canonical vendor → the labels acceptable on a given
# source so ONE CFB_VENDOR drives both portals. We accept BOTH "MLJQ" and "MLJC" on US so
# the pipeline keeps working before AND after the portal is corrected (idempotent to the
# rename). Matched case-insensitively; the canonical name is always tried too.
CFB_VENDOR_SOURCE_ALIASES = {
    "MLJQ": {"US": ["MLJQ", "MLJC"]},
}
# Weekly sales → Journal starts at this week-ending Friday (the first week the tool posts;
# earlier weeks were entered by hand). The catch-up job posts every complete week from here
# to now that isn't already posted, so a laptop closed for weeks backfills correctly.
CFB_SALES_ANCHOR = "2026-08-21"

# --- Which sellers' inventory to capture, and where each lands -----------------
# The monthly inventory capture reads ONE batch report per seller and posts its value to
# that seller's own column in the financial "Résultats mensuels" sheet (1-based col; A=1):
#   Binobrick → V (22)   ·   MLJQ → W (23)
# `sommaire` = also update the accounting "Sommaire mensuel" B/D (Inventaire estimé /
# Pièces) — that summary is MLJQ's own operational inventory, so only MLJQ writes it.
# `vendor` is matched case-insensitively against the portal's vendor dropdown. MLJQ isn't a
# seller on the portal yet: a capture run SKIPS (logs) any vendor the portal doesn't offer
# instead of failing, so Binobrick posts now and MLJQ auto-activates once it exists.
# `untreated`: ajouter a la valeur postee ce qui est CHEZ NOUS et pas encore au portail —
# les sets achetes, tries, emballes, que CFB n'a pas encore saisis (voir sortpack/untreated.py).
# Sans ca, acheter de l'inventaire fait BAISSER la Fortune : l'argent sort de l'encaisse et la
# marchandise n'apparait nulle part. Reserve a MLJQ : l'inventaire Binobrick n'est pas le notre.
CFB_INVENTORY_VENDORS = [
    {"vendor": "Bino", "fin_col": 22, "sommaire": False},                     # V — Binobrick
    {"vendor": "MLJQ", "fin_col": 23, "sommaire": True, "untreated": True},   # W — MLJQ
]

# --- Value history (our own SQLite) ------------------------------------------
# We do NOT duplicate BrickStore's catalog — the blob IS the catalog DB and we query
# it live. This SQLite only stores what the blob can't: dated snapshots of set values
# and per-part prices, so the "what to buy" report can look at trends over time.
HISTORY_DB = os.path.join(_PROJECT_ROOT, "history", "mljq_history.sqlite")

# --- Paths -------------------------------------------------------------------
# BrickStore's local catalog file (holds per-part weight AND every set's parts list
# / colors / item types). Refreshed when you run BrickStore's "Update Database".
# This is read-only; we never write to it.
_BRICKSTORE_CACHE = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")),
    "BrickStore", "cache")
BRICKSTORE_DB = os.path.join(_BRICKSTORE_CACHE, "database-v12")

# BrickStore's cached BrickLink price guide (sqlite). Drives the set-value feature.
# Only as fresh as the last time BrickStore updated prices online — the value report
# shows this file's timestamp so you always know the price date.
BRICKSTORE_PRICEGUIDE = os.path.join(_BRICKSTORE_CACHE, "priceguide_cache.sqlite")

# The Google Drive mount root differs per machine (e.g. "G:\Mon Disque" on one laptop,
# "C:\Users\jerem\Google Drive" on another). This project folder lives directly under that
# root, right next to the "BSX" folder, so we DERIVE the root from this file's location
# instead of hard-coding a drive letter — that makes the tool portable across machines with
# no edits. Set the env var MLJQ_DRIVE_ROOT to override (e.g. if the layout ever changes).
DRIVE_ROOT = os.environ.get("MLJQ_DRIVE_ROOT") or os.path.dirname(_PROJECT_ROOT)

# Root folder that contains one subfolder per set (the "cataloguage" output).
# Centralized under Google Drive's BSX\CFB. Only subfolders whose name starts with
# a digit (the set number, e.g. "76342 - ...") are treated as sets; anything else
# in here (backups, the CFB output files, desktop.ini, …) is ignored.
CATALOGUAGE_ROOT = os.path.join(DRIVE_ROOT, "BSX", "CFB")

# Before writing remarks, each bag file is copied here first (into a timestamped
# run subfolder mirroring the set folder), so a pristine pre-write copy always
# exists. Set ENABLE_BACKUP = False to skip. Kept inside BSX\CFB so everything is
# centralized in one Google Drive folder.
ENABLE_BACKUP = True
BACKUP_ROOT = os.path.join(CATALOGUAGE_ROOT, "backups")

# --- Bagging (Phase A: final "Sac" assignment) -------------------------------
# Target weight per outgoing bag, in grams (~650 g). A bag is closed once adding the next
# lot would push it over this (soft target: a single heavy lot still gets its own
# bag). Sort order within the whole inventory before bagging:
SAC_TARGET_GRAMS = 650.0
SORT_KEYS = ("type", "category", "name", "color")  # Type -> Category -> Description -> Color
# Item types are packed in this order (first listed = lowest Sac numbers). Minifigs
# come before Parts, so minifigs land in Sac 1; the rest follow. Any ItemTypeID not
# listed here sorts after these, by its own id. Values are BrickStore ItemTypeID
# letters: M=Minifig, P=Part, S=Set, B=Book, G=Gear, C=Catalog, I=Instruction,
# O=Original Box.
ITEM_TYPE_ORDER = ("M", "P", "S", "B", "G", "C", "I", "O")

# --- Consolidation (Phase B: C-bins while sorting through numbered bags) ------
# A consolidation bin (C1, C2, ...) is a physical BOX that holds several in-progress
# colours as separate small bags (capacity = how many at once). "new vs conso" is tracked
# PER COLOUR: a colour's first bag says "C01" (new small bag in box C01), its later bags
# say "C01 conso" (this colour is already partially in C01). So a colour can never be
# "conso" in its first bag.
# MAX_CONSOLIDATION_BOXES est un PLAFOND de boîtes physiques, pas un découpage. Une couleur
# à ramasser va dans la boîte qui contient le MOINS de petits sacs à cet instant (_Bins dans
# plan.py), et une boîte qui se vide redevient la prochaine choisie — une fenêtre glissante.
#
# Avant, la capacité était figée à ceil(total / MAX_BOXES) et on remplissait dans l'ordre :
# sur un set à 72 couleurs, les douze premières allaient toutes dans C01, les douze suivantes
# dans C02, etc. Comme les couleurs arrivent dans l'ordre du tri, les premières boîtes
# restaient les plus chargées du début à la fin — on fouillait toujours dans C01 pendant que
# C05 était presque vide. Mesuré sur 11503 : pics 12/12/12/12/12/7 (écart 5) avec l'ancienne
# règle, contre 12/11/11/11/11/11 (écart 1) avec l'équilibrage.
#
# Vaut pour les boîtes C (lot) comme pour les boîtes D (couleur). Les vieilles constantes
# CBIN_CAPACITY / BBIN_CAPACITY ne servent plus.
# Un BATCH sollicite bien plus les boites C qu'un set seul : 168 couleurs a ramasser et un
# pic de 80 petits sacs ouverts, contre une dizaine pour un set. 6 boites -> ~13 petits sacs
# chacune au lieu de ~27, ce qui reste fouillable a la main. (Les boites D, elles, ne servent
# plus dans un batch : Phase A met deja les couleurs d'un moule dans le meme Sac.)
# Un batch se trie avec ses Sacs OUVERTS sur la table : leur nombre est connu dès le
# premier lot, donc une couleur finie y va directement et la boîte D ne sert plus à rien
# (Phase A met déjà les couleurs d'un moule dans le même Sac). Mettre à False pour revenir
# au cycle C -> D -> Sac, si un jour on trie un batch sans étaler ses Sacs.
BATCH_OPEN_SACS = True

# Les batchs vivent dans leur propre dossier : CFB/Batchs/<nom>/, qui contient le fichier de
# consolidation, le maitre une fois construit, et les DOSSIERS DE SETS du batch — un set
# rejoint un batch en y etant deplace. Le conteneur porte un nom qui ne commence pas par un
# chiffre, sinon is_set_folder() le prendrait lui-meme pour un set.
BATCH_ROOT = os.path.join(CATALOGUAGE_ROOT, "Batchs")

MAX_CONSOLIDATION_BOXES = 6
CBIN_CAPACITY = 20
BBIN_CAPACITY = 20

# --- Remark text templates ---------------------------------------------------
# A remark is a chain of destinations read left->right, joined by REMARK_ARROW. NEW vs
# "conso" is tracked PER COLOUR (for C) / PER MOULD (for D): the pure label is this
# colour/mould's FIRST appearance (start its bag), and " conso" means it is ALREADY
# partially in that bin (seen before) — so nothing is ever "conso" in its first bag.
#
# Symbol order under BrickStore's text sort of the Remarks column:  C (lot) < D (colour)
# < Sac.  So lot-consolidation rows sort first, then colour-consolidation rows, then the
# outgoing Sac rows. (Colour consolidation used to be "B"; renamed to "D" so it sorts
# AFTER lot consolidation "C".)
#
# Within one symbol's rows we want the MULTI-STEP finalize actions (those with an arrow)
# to appear BEFORE the plain single-step staging rows. A single-step staging label
# therefore carries a trailing STAGE_MARK ("*"): '*' (0x2A) sorts after the space that
# opens " -> " (0x20) but before the '-' (0x2D), so "C02 conso -> Sac 05" sorts ahead of
# "C02 conso*" / "C02*". Terminal "Sac NN" rows never take the mark (keeps the Sac
# dividers sorting to the end of their block).
#
# A D BOX is only used when a multi-colour mould's colours FINISH in DIFFERENT bags — the
# box exists to park a colour that is DONE while its sisters are still coming, so what
# counts is where each colour finishes, not every bag it passes through. If they all finish
# in the SAME bag there is nothing to park, even when some of them arrive there from a C
# box: the mould is complete in that bag and its colours ship straight to their shared Sac
# as an adjacent GROUP row "Sac 05 (Red, Blue)" (same remark on every lot, so BrickStore
# sorts them together — grab the group, no hunting). Judging it by the bags the mould
# APPEARS in instead sent a colour catalogued in a single bag through a pointless D box and
# gave it its sisters' row, telling the sorter to park a mould that was already finished.
# A mould's FIRST bag into box D03 is a bare "D03" (nothing there yet — you're starting the
# box, not consolidating into it). LATER bags are "D03 conso (colours already in the box from
# EARLIER bags)" — the parens are what's there BEFORE you add this bag's colours, so you can
# spot the bag. All colours of the mould finishing in the SAME bag share the same "prior", so
# they get the identical remark and sort adjacent (a group); the Item Id subsort keeps a
# bare-"D03" group together too. Examples:
#   Sac 05                          one bag, single-colour mould
#   Sac 05 + (Red, Blue)            this bag also holds Red and Blue of the same mould:
#                                   combine the three, then into Sac 05
#   C02*                            this colour's first bag: start its bag in box C02
#   C02 conso*                      this colour is already partially in C02: add to it
#   C02 conso -> Sac 05             colour finished, bag it
#   D03                             first bag: put this colour into box D03 (nothing there yet)
#   D03* + (Black, Red)             ... and Black and Red are in THIS bag too: they all go
#                                   into the same D03 bag, so go find them first
#   D03 conso (Green)               later bag: box already holds Green -> add this colour
#   C02 conso -> D03 conso (Green)  a colour finished (from C02) joins box D03 (holds Green)
#   D03 conso (Green, Red) -> Sac 05   last bag: find the (Green, Red) bag, empty into Sac
#
# Read the two colour lists differently: "(…)" after "conso" is what the box ALREADY holds
# from earlier bags (how you recognise the bag), while "+ (…)" is what arrives WITH this lot
# in this same bag and has to be combined with it now. Rows sharing a D box in one bag can
# read differently — one of them may be coming out of a C box — so they cannot be found by
# sorting adjacent; each one names its siblings instead.
# Numbers are zero-padded to LABEL_PAD digits so the text sort orders them right
# (Sac 02 before Sac 10). {k} = bin number, {n} = Sac. The STAGE_MARK (an invisible U+00A0)
# trails the single-step staging rows; a "-> Sac" finalize row never takes it, so finalize
# rows sort ahead of staging rows.
LABEL_PAD = 2
SAC_LABEL  = "Sac {n}"      # final outgoing bag
CBIN_NEW   = "C{k}"         # this colour's FIRST bag: start its bag in box C{k}
CBIN_ADD   = "C{k} conso"   # this colour is already partially in C{k}: add to it
BBIN_NEW   = "D{k}"         # this mould's first finished colour: start its colour-bag
BBIN_ADD   = "D{k} conso"   # this mould's bag is already in D{k}: add this colour
REMARK_ARROW = " -> "
# Trailing mark on single-step staging rows so multi-step (arrow) rows sort ahead of them.
# It must sort AFTER the space that opens " -> " so the "-> Sac" finalize rows come first.
# A non-breaking space (U+00A0) does this in BrickStore's code-point sort AND is invisible
# (renders like a space) — prettier than a "*". If the ordering ever looks wrong, set this
# back to a visible char such as "*". Set to "" to disable the multi-step-first ordering.
STAGE_MARK = " "
# Colour hint on D (colour-consolidation) rows: " (Red, Blue)". Set to "" to disable.
# It lists what is ALREADY in the box, from EARLIER bags — so you can spot the right bag.
COLOR_HINT_FMT = " ({colors})"
COLOR_HINT_SEP = ", "

# SIBLING hint — the colours of the same mould arriving in THIS SAME bag, i.e. the ones to
# COMBINE with right now. It always trails the whole remark, so one symbol means one thing
# everywhere: "+ (…) = another colour of this mould is in this bag too; put them in the same
# bag before going on." Without it, several colours reaching a D box at once each read a bare
# "D01" and the sorter files them as separate little bags — and when one of them arrives from
# a C box its remark differs, so they do not even sort next to each other. Distinct from
# COLOR_HINT_FMT on purpose: the parens are the box's EXISTING contents (earlier bags), the
# "+" list is what joins NOW. Set to "" to disable.
SIBLING_HINT_FMT = " + ({colors})"

# Sac divider: in each bag file, ONE lot per Sac carries this remark instead of the
# plain SAC_LABEL, so that after you click-sort the Remarks column each Sac block ends
# with a visible line (e.g. "Sac 06 -----------") separating it from the next Sac. Keep
# the SAME "Sac {n}" prefix and case as SAC_LABEL so the divider sorts to the end of its
# own Sac group; a different case (e.g. "SAC") can scatter the dividers to the top of the
# list depending on BrickStore's sort mode. Set to "" to turn dividers off.
SAC_MARK = "Sac {n} -----------"

# Restants stage (point 3, "Calculer les restants"): when a leftover in the Inventory
# file belongs to a consolidation or a colour group, we warn with the ACTION TYPE and its
# destination Sac (no bin number — by this final stage the physical C/D bins have all been
# reused). The D (colour-group) case behaves like a conso and lists the mould's colours so
# you know what to look for when regrouping the leftover with its sisters, e.g.
# "C -> Sac 06" or "D conso (Green, Red) -> Sac 06" or "C -> D conso (Green, Red) -> Sac 06".
RESTANT_CBIN = "C"   # leftover of a colour that was consolidated across bags
RESTANT_BBIN = "D"   # leftover of a multi-colour mould (regroup with its sisters)

# The phone's per-set helper also lists the set's CATALOGUE parts that no file holds any
# more, so a lot deleted from a bag can be put back when the piece turns up (absent=True in
# restants_<set>.json, handled by restants.add_from_catalog). It costs one catalog read per
# write of that file; set to False to skip it and list only what the files contain.
BAG_HELP_INCLUDE_CATALOG = True

# « Construire CFB » from the PHONE finishes the set the way the desktop's
# « Traiter le set ? » does: once the master has actually been EMAILED, the copies leave
# stock (history.send_to_cfb — idempotent and reversible by deleting the `sent` row) and the
# set folder goes to the Recycle Bin (recoverable; sortpack/trash.py never hard-deletes).
# Nothing is touched when the mail did not go out, or when the pre-send check found a gap it
# could not correct — the folder is the only evidence left at that point. Set to False to
# keep the phone building only and finish sets on the desktop.
CFB_PHONE_AUTO_PROCESS = True

# Validate a set one last time when its CFB master is BUILT, not only at Apply. Apply is
# normally where the leftovers are brought to ×N and the counts reconciled — but auto-apply
# skips a set as soon as its sorting has started, so a set can reach the master having never
# been through an Apply. Its leftovers would then ship at 1/N (finalize folds the inventory
# in as-is) and any miscount would ship with them. Set to False to build without checking.
CFB_VERIFY_ON_BUILD = True

# --- Column display (GuiState) -----------------------------------------------
# BrickStore stores the per-file view (which columns show, their widths/order, and the
# active sort/filter) as a <GuiState> block of compressed blobs. Bag files generated
# upstream often carry no/stale GuiState, so they open with the wrong columns. When
# OVERWRITE_GUI_STATE is on, every bag we write gets this known-good block injected
# (captured from a reference file the user confirmed displays correctly — columns +
# sorted by Remarks), so the sorted view is ready the moment the file opens. The blobs
# are opaque BrickStore state (qCompress: 4-byte size + zlib) copied verbatim; they are
# generic view state (not row-specific), so they transplant safely between bag files.
OVERWRITE_GUI_STATE = True
# The known-good <GuiState> block lives in sortpack/gui_state.xml (captured from a user-
# configured file: the columns arranged/sized as wanted, plus a 3-level sort Remarks ->
# Item Id -> Color so a bare-"D01" group clusters by part then colour). To re-capture: set
# the view in BrickStore, save, and copy that file's <GuiState>…</GuiState> block into
# sortpack/gui_state.xml. Loaded here so the long compressed blobs never live inline.
def _load_gui_state():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_state.xml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip("\n")
    except OSError:
        return ""

BSX_GUI_STATE = _load_gui_state()

# When a set folder is auto-created (a new purchase synced), drop a fresh full-set
# inventory .bsx into it from the BrickStore catalog, so cataloguing starts from the whole
# set and you catalogue by moving parts OUT of it into the numbered bags. Named
# "Inventory for <set-id>.bsx" (matches SKIP_PREFIXES so the bag discovery ignores it).
AUTO_CREATE_INVENTORY = True
# The inventory file gets its OWN known-good column/sort layout (different from the bag
# files' BSX_GUI_STATE) — captured from a user-configured inventory file. Lives in
# sortpack/inventory_gui_state.xml so the long compressed blobs never sit inline.
def _load_inventory_gui_state():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "inventory_gui_state.xml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip("\n")
    except OSError:
        return ""

INVENTORY_GUI_STATE = _load_inventory_gui_state()

# --- File discovery ----------------------------------------------------------
# Files whose name starts with any of these prefixes are ignored (matches the
# Apps Script's "skip Inventory*" rule). Only *.bsx are ever processed.
SKIP_PREFIXES = ("Inventory",)

# --- Workflow status (GUI list) ----------------------------------------------
# The next action to do for a set, shown beside its name in the top list. The first two
# are derived automatically from the files present, the rest are driven by hand:
#   Cataloguer         → cataloguing not finished: the "Inventory…" file (the set's not-yet-
#                        catalogued parts) still holds more than CATALOGUE_DONE_FRACTION of
#                        the set. As you catalogue, parts move from Inventory into the
#                        numbered bags and the file shrinks.
#   Séparer les sacs   → AVANT tout le reste : répartir les sachets numérotés des N boîtes
#                        en piles par numéro. Ça doit se faire d'abord — une fois les
#                        sachets ouverts et versés, le cataloguage par sac est impossible.
#                        Se termine sur un geste (bouton du téléphone), parce qu'aucun
#                        fichier ne peut témoigner d'un travail purement physique.
#   Trier              → set by hand once the bags are open; then a per-bag checklist tracks
#                        progress, so the list shows "Trier (3/8)" as you go.
#   Trié               → shown once every numbered bag is checked off (ready to build CFB).
# When a set is finished (CFB built) its folder is removed and it drops off the list.
# A physical numbered bag = the leading file number: "1.bsx" and "1 - 1.bsx" are both bag 1,
# and the bag COUNT is the biggest number seen (an "8.bsx" means the set has 8 bags).
STATUS_CATALOGUER = "Cataloguer"
# Remplace l'ancien « Ouverture des sacs », qui venait APRÈS le cataloguage. Le tri a
# changé : on sépare les sachets par numéro d'abord, et on catalogue ensuite pile par pile.
# Du coup l'ouverture n'est plus une étape à part — elle fait partie du cataloguage — et la
# seule chose qui doive être faite avant, c'est la séparation. Conséquence voulue : il ne
# reste que DEUX phases qui se chevauchent, « Cataloguer » et « Trier ».
STATUS_SEPARER = "Séparer les sacs"
# La séparation est purement physique : aucun .bsx, aucune base, rien ne peut la constater.
# Elle se clôt donc sur un geste, rangé ici sous la clé `separated` de set_status.json.
# Garde-fou : un set qui a déjà des sacs numérotés est forcément séparé (on n'aurait pas pu
# les cataloguer autrement), donc la pastille ne le réclame pas — sinon un set d'avant ce
# changement, ou un cataloguage commencé sans avoir cliqué, resterait coincé dessus.
STATUS_TRIER = "Trier"
STATUS_TRIE = "Trié"
# Apres le tri, deux etapes qui ne se devinent pas des fichiers :
#   PRET  - pose automatiquement quand le MAITRE du batch est construit. Le set est emballe,
#           il attend le transporteur. Son dossier reste visible : on peut encore l'ouvrir.
#   LIVRE - pose a la main quand l'envoi est parti. C'est LUI qui solde : les copies sortent
#           du stock, le dossier va a la corbeille, et le set quitte la liste pour l'onglet
#           Historique (qui se lit dans la table `sent`, pas sur le disque).
# Un set d'un envoi PAS ENCORE FERME ne peut pas etre trie : tant qu'un set peut encore
# entrer, la numerotation des Sacs n'est pas figee, et trier reviendrait a ensacher selon des
# numeros qui vont changer. Il attend donc ici, et bascule tout seul en « Trier » — remarques
# appliquees — des que l'envoi est ferme (batch.settle_phases).
# Pose a l'achat, retiree quand un courriel de livraison arrive (sortpack/deliveries.py).
# Sans elle, un set achete apparaissait en « Cataloguer » des que la synchro creait son
# dossier — alors que la boite etait encore chez le transporteur.
STATUS_LIVRAISON = "En attente de livraison"
STATUS_ATTENTE = "En attente de fermeture du batch"
# L'autre attente, a l'autre bout du tri : ce set-la est FINI (catalogue, tous ses sacs
# coches), mais l'envoi auquel il appartient ne l'est pas. Il n'y a plus rien a y faire et
# il ne partira qu'avec les autres — « Trie ✓ » le laissait croire pret a expedier, alors
# que le maitre couvre tout l'envoi. Phase DEDUITE (gui.status_for), jamais ecrite dans
# set_status.json : ce qui y est stocke reste « Trier » + bags_done, dont dependent
# sorting_started et l'epinglage du plan.
STATUS_ATTENTE_RESTE = "En attente du reste du batch"
STATUS_PRET = "Prêt pour livraison"
STATUS_LIVRE = "Livré"
# Cataloguing is considered done — and the set flips from "Cataloguer" to "Ouverture des
# sacs" — once the leftover Inventory file is this fraction (or less) of the set's parts
# (Inventory + numbered bags). 0.05 = "the Inventory is under 5% of the set".
CATALOGUE_DONE_FRACTION = 0.05

# --- Verification of the hand-cataloguing (sortpack/verify.py) ---------------
# Cataloguing moves parts out of the leftovers Inventory file and into the numbered bags;
# decrementing one and incrementing the other is where a piece goes missing or gets counted
# twice. Before the remarks are written, the files are compared against the set's real
# inventory (BrickStore's catalog, spares INCLUDED - the box physically contains them) and
# the difference is put back: missing pieces go to the Inventory lot if there is one, else
# to any bag lot, else a new Inventory lot; surplus comes off the Inventory first, then the
# bags. Set False to only report, never write.
VERIFY_ON_APPLY = True
# A lot the catalog doesn't know at all (instructions, box, a part typed by mistake) is
# removed. Set False to report those and leave them alone.
VERIFY_DROP_UNKNOWN = True
# Safety valve: a large gap means the SET is wrong (wrong number, files from another set),
# not the count - past these the pass reports and writes nothing.
# (a real miss rate can be high - 11503 had 31 of its 97 lots wrong, 32% - while a wrong
# set shows ~100%, so these sit in between.)
VERIFY_MAX_LOT_FRACTION = 0.50    # share of the set's lots allowed to differ
VERIFY_MAX_QTY_FRACTION = 0.25    # share of the set's pieces allowed to differ
# The per-set workflow state — the manual "Trier" bump and the checked-off bag numbers — is
# remembered here, keyed by set number, e.g. {"42680": {"status": "Trier", "bags_done": [1]}}.
# It lives INSIDE the CFB folder (on Google Drive) so the phone web-app can read/write the
# same file: bag check-offs sync both ways between the desktop and the phone via Drive.
SET_STATUS_FILE = os.path.join(CATALOGUAGE_ROOT, "set_status.json")
# A small snapshot the desktop writes on every refresh (set name, number, base status and
# bag count per set), so the phone web-app can list the sets without parsing any .bsx. Also
# in the CFB folder. Read-only for the phone; rewritten each desktop refresh.
SORT_INDEX_FILE = os.path.join(CATALOGUAGE_ROOT, "sort_index.json")
# Per-set helper the phone reads while sorting a numbered bag: the leftovers list with its
# physical quantities, each one's destination Sac, and the bags that still hold that part or
# its sister colours - enough for the phone to answer "I'm at bag N, where does this go?"
# without asking the PC. Written at Apply (the plan is already in hand) and refreshed
# whenever the inventory changes. {set} = the set number.
BAG_HELP_FILE = "restants_{set}.json"

# The phone's Finances / Achats tabs read ONE more snapshot the PC writes beside the index:
# the ranked buy list, the budget ("reste a investir"), the CFB inventory values, the cookie
# status and the freshness ages. Same folder, same read-only contract (sortpack/mobile.py).
# It is rebuilt after every processed phone command and, at most every
# MOBILE_DASHBOARD_REFRESH_SECONDS, by the desktop poll.
MOBILE_DASHBOARD_FILE = os.path.join(CATALOGUAGE_ROOT, "dashboard.json")
MOBILE_DASHBOARD_REFRESH_SECONDS = 600
# How many non-chosen candidates ride along after the monthly batch (the phone's "voir tout").
# 0 = all of them — the Achats tab searches/filters/sorts the whole ranking, so it needs the
# whole ranking (~280 rows, a few tens of kB of JSON).
MOBILE_DASHBOARD_EXTRA_ROWS = 0
# Combien de sets livres l'onglet Historique du telephone recoit. 0 = tous : la table `sent`
# grossit d'une ligne par set expedie, soit quelques dizaines par an — rien a tronquer.
MOBILE_HISTORY_ROWS = 0

# --- Phone → PC command queue (mobile "Appliquer" / "Construire CFB") ---------
# The phone can request the heavy steps; the PC runs them (GUI background poll, the
# "Traiter les demandes" button, or `python -m sortpack.remote` from Task Scheduler).
# When True, the running GUI processes queued phone requests automatically every
# REMOTE_POLL_SECONDS. Set False to only process them on the button.
REMOTE_AUTO_PROCESS = True
REMOTE_POLL_SECONDS = 25

# --- Single-owner lock (sortpack/lock.py) ------------------------------------
# The CFB folder is on Google Drive, so every machine with the project installed sees the
# same files. Two pollers means both process the queue and both auto-apply, and whichever
# Drive sync lands last wins - that is what re-applied three finished sets on 2026-09-23
# at 12:11 from a second machine holding a stale copy. So one machine owns the work:
# poller_owner.json in the CFB folder carries its name and a heartbeat, the others stand
# down, and a lock left cold for POLLER_LOCK_STALE_MINUTES can be taken over so the work
# never stalls for good. Courtesy lock, not a mutex - Drive latency means a simultaneous
# start can still overlap; nothing destructive rests on it alone.
POLLER_LOCK = True
POLLER_LOCK_FILE = os.path.join(CATALOGUAGE_ROOT, "poller_owner.json")
POLLER_MACHINE_NAME = ""          # blank = this machine's hostname
POLLER_LOCK_STALE_MINUTES = 10    # a lock this cold may be taken over
POLLER_HEARTBEAT_SECONDS = 120    # how often the owner re-stamps it (Drive write)
# Set True on the PC that should ALWAYS win (it takes the lock even from a live owner).
POLLER_FORCE_OWNER = False
# A remote "Construire CFB" emails the master/restants files to Canada First Brick (the whole
# point of building them). Set False to only write the files and send them by hand.
REMOTE_CFB_EMAIL = True
# Auto-write the remarks (no button): once a set is done cataloguing (Inventory ≤
# CATALOGUE_DONE_FRACTION — 5%, i.e. that set is finished), the PC writes the remarks into
# the bag files by itself — the same headless engine that runs the other jobs. Runs from the
# GUI poll and from `python -m sortpack.remote`, at any time: it no longer waits for
# BrickStore to be closed. The decision reads the FILES, via a "<lots>/<stamped>" fingerprint
# stored in auto_apply.json:
#   • bags already fully remarked  -> skipped, no catalog load, no backup, no rewrite
#     (this is how a set applied from the desktop button is recognised);
#   • fingerprint changed since we wrote (re-catalogued, or BrickStore saved an old tab over
#     our remarks) -> written again, so nothing stays lost;
#   • leftovers Inventory already through "Calculer les restants" -> never touched again:
#     the sorting is physically under way and the Sac layout must not be recomputed.
# A set with no logged purchase quantity is skipped (its ×Quantité is unknown) — log the
# purchase first.
REMOTE_AUTO_APPLY = True
# Le meme service pour un ENVOI (sortpack/batch.auto_apply_all). L'auto-apply par set ne
# touche jamais un set de batch — il le planifierait seul, avec une numerotation de Sacs a
# lui — donc sans cette passe les remarques d'un envoi ne se reecrivaient qu'au bouton.
# Elle ne regarde que les envois FERMES (composition figee, donc numerotation figee) et
# n'ecrit que quand l'empreinte de leurs sacs a bouge : un sac de plus parce que le
# cataloguage avance, des remarques perdues, ou le cataloguage qui vient de se terminer.
# C'est ce qui permet de cataloguer un set et de trier ses premiers sacs dans les memes
# Sacs, sans rien relancer. Tant qu'un set n'est pas fini d'etre catalogue, ses sacs sont
# annotes mais PAS multiplies (batch.apply) : les numeros de Sacs sont les memes de toute
# facon, et un sac a moitie saisi qu'on aurait multiplie laisserait les lots suivants a x1
# dans un fichier tamponne xN.
BATCH_AUTO_APPLY = True
# Combien de temps un fichier de l'envoi doit etre RESTE TRANQUILLE avant que la passe le
# reecrive. La question « ce sac est-il fini ? » n'a pas de reponse dans les fichiers : on
# saisit un sac lot par lot et BrickStore enregistre quand on le lui demande. Le seul signal
# honnete est donc « plus personne n'y a touche depuis un moment ». Sans ca la passe tombe
# au milieu d'une saisie, annote la moitie d'un sac, et BrickStore ecrase tout au prochain
# enregistrement : rien de casse (l'empreinte fait tout reecrire au passage suivant), mais
# des remarques qui apparaissent et disparaissent sous les yeux, et une sauvegarde pour
# rien a chaque tour. L'attente ne retarde que l'ecriture automatique — le bouton
# « Appliquer le batch » n'en tient aucun compte.
BATCH_APPLY_QUIET_SECONDS = 120

# --- Per-set quantity multiplication (at Apply time) -------------------------
# The numbered bag .bsx now arrive at BASE quantities (x1 per copy of the set).
# When you Apply, this project multiplies every <Qty> by how many copies we own —
# read from our SQLite `inventory` table, which is populated from the Journal's
# Catégorie-A purchases (sortpack/purchases.py). Weights / Sac assignment use the
# multiplied quantities too. (Name kept for compatibility; source is now the DB.)
#
# Idempotence: after multiplying, each bag file is stamped with a marker so a second
# Apply won't multiply again. Regenerating the bags from the cataloguage (fresh x1)
# clears the marker. Set to False to disable and keep quantities as-is.
CFB_MULTIPLY_FROM_SHEET = True

# --- CFB finalizer (Phase C: build the file to send to Canada First Brick) -----
# Where the consolidated "<set-number>.bsx" (master) and "<set-number> - restants.bsx"
# are written when you hit "Construire fichier CFB".
CFB_OUTPUT_DIR = CATALOGUAGE_ROOT

# A lot whose <Remarks> equals exactly this (trimmed, case-insensitive) is treated as
# physically MISSING from that bag: its quantity is moved to the restants file.
CFB_MISSING_REMARK = "x"

# --- Emailing the CFB files via a Google Apps Script Web App -------------------
# Deploy the Apps Script (see appscript/CFB_email.gs) as a Web App, then paste its
# /exec URL here and set the same shared secret in both places. Leave the URL empty
# to disable emailing (the button then just writes the files).
CFB_APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbzzmFzyiDScqG5vW05Mi3G5m_lD5ec9afGQJpq68yvNWLmLJFhQ3lAnNpEGRljj0ixN/exec"
CFB_APPS_SCRIPT_TOKEN = "zxzxws12"   # must match SECRET in the Apps Script
CFB_EMAIL_TO = "jeremie.queenton@gmail.com"
