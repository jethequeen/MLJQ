# MLJQ – Sort and Pack Helper

Writes packing **remarks** into BrickStore `.bsx` bag lists so a sorter can
consolidate and bag a set's parts. Runs entirely offline — **BrickStore does not
need to be open.**

## How to run

Double-click **`Sort and Pack.bat`** (or `python gui.py`): pick the set you just
finished cataloguing → **Preview plan** → **Apply → write remarks**.

Command line:

```
python run.py "<set folder>"            # dry-run, writes nothing
python run.py "<set folder>" --apply    # write remarks (backs up first)
python run.py --all [--apply]           # every set under the cataloguage root
```

## Installer sur un autre ordinateur

Le lanceur **`Sort and Pack.bat`** est portable (chemins relatifs) — copie-le sur le
Bureau via clic droit → *Créer un raccourci*. Sur une nouvelle machine :

1. **Installer Python 3** depuis <https://www.python.org/downloads/> en cochant
   *« Add python.exe to PATH »*. (tkinter et sqlite3 sont inclus — rien de plus.)
2. Dans le dossier du projet : `pip install -r requirements.txt`
   (seulement `requests` + `beautifulsoup4`, pour les scrapers ; le tri/emballage
   tourne même sans).
3. Double-cliquer `Sort and Pack.bat`.

**Chemins spécifiques à la machine** (dans `sortpack/config.py`) : le cache BrickStore
est trouvé automatiquement via `%LOCALAPPDATA%`, mais `CATALOGUAGE_ROOT` / `BACKUP_ROOT`
pointent sur `G:\Mon Disque\BSX\CFB` (Google Drive). Si Drive se monte sur une autre
lettre, ajuste ces chemins. Il faut aussi **BrickStore installé** (pour le catalogue des
poids) et Google Drive synchronisé.

## Set part-out value (offline, no XML)

The **Valeur du set** button (and `value.py`) computes a set's part-out value by
reading BrickStore's own database directly — the catalog blob for the set's parts
and `priceguide_cache.sqlite` for prices. No BrickStore process, no BSX export.

```
python value.py 76342                    # one set: min / avg / qty-avg / max / 6-month / used / VP
python value.py explain 76342            # full breakdown: recovery, liquidity, momentum, history, top parts
python value.py explain P3001@5          # same, for one part/colour (key = <type><id>@<colour>)
python value.py --all --out report.csv   # every set -> CSV (raw data for the buy report)
python value.py --all --min-year 2020 --out report.csv
```

Buy-report economics (fee, USD→CAD, tax handling, budget, ROI floor) live in
`sortpack/config.py`. Net recovery is modelled on **6-month sold** prices minus the
CFB fee; purchase tax is excluded (recovered as input tax credits). **All amounts
shown are CAD** (the DB stores USD; display converts at `USD_TO_CAD`).

### Ranked buy list

```
python value.py buy                       # rank Amazon-discounted sets, pick the monthly batch
python value.py buy --out buy_list.csv    # + write the full ranked list
python value.py buy --top 60              # print more rows

python value.py job                       # HEADLESS pipeline: scrape Amazon -> history -> rank -> DB
python value.py job --post                # ...and POST the buy list to the Google Sheet (if configured)
```

The **job** is the automated path (also the GUI **"Rapport d'achat"** button and a weekly
scheduled task). It's **DB-only** — everything lives in `history/mljq_history.sqlite`, no CSVs.
Two discount feeds:

- **Amazon** (`sortpack/amazon.py`) — headless scrape of Brickset, no browser. Stored in `amazon_price`.
- **Costco** (`sortpack/costco.py`) — headless too: the website blocks headless browsers, but its
  JSON product API (`gdx-api.costco.com/catalog/search`) needs no cookies, so plain HTTP works.
  Pulls the Building Blocks & Sets → LEGO listing (~186 in-stock SKUs, prices are your local QC
  Costco). Stored in `costco_price`. No browser required.

The job keeps **one row per real offer** — a set sold at both stores appears twice, with each
store's actual price, a `source` column (`amazon`/`costco`), and a `url` buy-page link (Amazon
`/dp/<ASIN>`, Costco's product page; the sheet renders it as a clickable **Acheter** link). The
batch just won't buy the same set twice. It ranks them, stores the list in `buy_list`, and — when `BUY_REPORT_POST_URL`
is set — POSTs it to the sheet's *Achats* tab by **reading it back from the DB**. `python value.py
buy` shows the latest ranking. The **MLJQ Weekly Buy Report** task runs it every Monday (fully
headless). To enable the sheet POST, redeploy `appscript/CFB_email.gs` and set `BUY_REPORT_POST_URL`.

Reads the Amazon discount feed (`config.AMAZON_CSV`: `ID, Theme, Price, Discount`),
computes `ROI = net_recovery_CAD / cost_CAD` for each set, keeps those clearing
`ROI_FLOOR`, ranks, and greedily fills the monthly batch (`MONTHLY_BUDGET_CAD`,
`BATCH_SIZE`, with `BUDGET_FLEX` for standouts). `sortpack/buylist.py` holds the logic.
The **achat max** column is the highest price to pay and still clear the floor.

**Amount vs speed.** The buy decision separates the two:

- **How much** — `net_recovery` = part-out on the recovery basis (6-month sold prices)
  × a **flat `REALIZATION_RATE`** (~70%, the fraction of value realized over ~5 years,
  before the CFB fee). Roughly set-independent; `SetValue.recovery_value` in
  `sortpack/setvalue.py`.
- **How fast** — `weighted_ratio` (`sold-6mo / listed`) → `months_to_liquidate`.

The buy list ranks by **capital efficiency — profit per dollar per year** =
`(ROI − 1) × 12 / months` — so a cheap fast flip beats a big slow one (the returned
capital compounds into the next batch). It's gated on plain `ROI ≥ ROI_FLOOR` first.
Every threshold is a knob in `sortpack/config.py` (`REALIZATION_RATE`,
`SELL_WINDOW_MONTHS`, `ROI_FLOOR`, …); the buy table shows `mois` (est. months to
liquidate), `%/an` (the ranking metric), and `$/an` (absolute annual profit, for context).

Besides the raw part-out metrics, it reports **VP (valeur pondérée)** and
**Turnaround** — the liquidity-adjusted value from the reference `selling_ratios.py`
(a part keeps its value only insofar as it actually sells: `vp = avg·qty·1.3555^(-1/(4·ratio))`,
`ratio = sold-6mo / listed-now`). VP is the "what to buy" signal.

Extras, alternates and counterparts are excluded. Prices are only as fresh as
BrickStore's last online price update — every report shows that date. Format lives in
`sortpack/catalogdb.py` (catalog blob), `sortpack/priceguide.py` (price layout),
`sortpack/setvalue.py` (value join).

### History (`history/mljq_history.sqlite`)

Our own SQLite of dated snapshots — the one thing the blob can't give us. The catalog
is never duplicated (it *is* the catalog DB, queried live); this only stores time series.

```
python value.py --all --snapshot     # today's value of every set -> set_value table
python value.py --snapshot-prices    # the whole price guide -> part_price table
```

`part_price` is keyed by the price-cache date (idempotent — re-running the same day is a
no-op); `set_value` is keyed by snapshot date. Run these on a schedule to accumulate the
trends the daily "what to buy" report will compare against retail/discount data. See
`sortpack/history.py` (`set_value_series`, `part_price_series`).

## Phone app (Apps Script web-app)

`appscript/SortPack_mobile.gs` + `SortPack_mobile_page.html` are a private web-app you add to
your home screen. It never parses a `.bsx`, never opens the history DB and never reads the
sheet: the PC writes three small JSON files into the Drive CFB folder and the phone reads
them (`sort_index.json`, `set_status.json`, `dashboard.json`). Three tabs:

- **Tri** — the sets and their phase, one compact row each. Tap a set to open it: a check-off
  chip per numbered bag (two-way with the desktop) and the buttons that queue *Appliquer
  restant* / *Construire CFB* for the PC. Cards start collapsed — the list is for finding the
  set you are on, and a screenful of bag chips buried it.
- **Achats** — the latest ranked buy list: the **lot du mois** first (rank, store, prix,
  lot ×qté, ROI, %/an, $/an, mois, a buy link), then the runners-up on demand.
  Chaque carte montre **ROI et Pièces côte à côte** : le premier dit si le set vaut la
  peine, le second s'il remplit l'envoi. La grille fait 3 colonnes, donc « Lot » descend
  d'une rangée et le délai de revente prend la dernière en entier.
  A discounted offer shows its markdown — a green −N % pill on the card and the
  pre-discount price struck through under *prix*. Above the list, a bar searches the whole
  ranking by name / number / year, filters by store (Tous · Amazon · Costco) and sorts it
  by classement, %/an, ROI, rabais, prix (↑↓), délai de revente, profit $/an or nom.
  Touch any of the three and the tab flattens into one filtered list over **every** candidate
  (the batch keeps its accent rank badge) with a count and *Réinitialiser*; leave them alone
  and it keeps the lot-du-mois shape. `MOBILE_DASHBOARD_EXTRA_ROWS = 0` ships the whole
  ranking to the phone so the filters see all of it.
  *J'ai acheté* opens the purchase sheet prefilled with the set, the store and the quantity
  (never the price — the list carries the shelf price, the Journal wants what you paid).
  La ligne du Journal porte aussi **H Lots / I Pièces**, comme celle du bureau : le PC met
  dans `dashboard.json` les comptes de chaque candidat (`journal_lots`, `journal_pieces`) et
  la page envoie les premiers tels quels, les seconds multipliés par la quantité saisie.

  **La règle, vérifiée contre les maîtres réellement expédiés** (43011, 42680 et 77256
  tombent au lot près et à la pièce près) :

  - les **lots ne se multiplient pas** — 9 exemplaires du même set, ce sont les mêmes lots
    (pièce + couleur) avec 9× la quantité dedans ; et ils se comptent **sans** les pièces en
    trop, parce qu'une pièce en trop double presque toujours une pièce déjà au set : elle
    grossit un lot existant, elle n'en crée pas ;
  - les **pièces se multiplient**, et **comptent** les extras — la boîte les contient et CFB
    les reçoit. Par la quantité *saisie*, pas celle du classement.

  **Le batch.** Le modal porte un champ *Batch*, pré-rempli avec l'envoi que l'achat sert à
  compléter — le PC le nomme déjà dans `dashboard.json` (`buy_list.shipment.name`). Sous le
  champ, la page dit ce que la confirmation va faire en plus d'écrire au Journal :
  « En confirmant, le set sera automatiquement ajouté au batch 1. » Modifiable, et vide =
  aucun batch.

  **La synchro suit dans la foulée.** Un achat n'est pas fini quand la ligne est écrite :
  tant que `purchases.sync` n'a pas tourné, l'achat ne descend pas dans la base locale, le
  dossier CFB n'existe pas, et « reste à investir » ne bouge pas. `logPurchase` met donc
  `sync_purchases` dans la file du PC juste après. `queueCommand` refuse un job global déjà
  en attente, donc trois achats d'affilée ne font qu'une seule synchro — et elle relit tout
  le Journal, donc elle les couvre tous. Le toast dit lequel des deux cas s'est produit.

  C'est aussi ce qui rattache un achat au batch **sans** le champ *Batch* :
  `purchases._attach_to_batch` inscrit à l'envoi en cours tout set nouvellement acheté qui
  était **retenu** au dernier classement. Les deux chemins se complètent — le champ inscrit
  tout de suite et couvre les achats hors classement, la synchro rattrape le reste.

  L'inscription passe par `addToBatch_` dans le `.gs`, qui rejoue les règles de
  `sortpack/batch.add` : batch connu et ouvert, pas déjà dans un autre batch ouvert,
  idempotent, sous le verrou de `queueCommand`. Elle n'écrit que le **registre** — déplacer
  le dossier du set est le travail du PC (`batch.settle`), et à l'achat ce dossier n'existe
  pas encore : le set n'est même pas reçu. C'est le cas prévu d'un set inscrit avant d'être
  catalogué.

  Elle ne se déclenche **qu'après une ligne de Journal réussie** — inscrire un achat qui
  n'existe pas ferait planifier un envoi sur du vide — et son résultat est dit dans le toast
  (« ajouté au batch 1 », « déjà au batch 1 », ou l'erreur), parce qu'un refus silencieux
  laisserait croire le set inscrit.

  `gui.py` faisait les deux erreurs depuis le début (`len(parts) * qty`, extras exclus) : le
  bureau écrivait ×qté fois trop de lots et oubliait les extras dans les pièces. Corrigé aux
  deux endroits en même temps. Un set absent du classement part sans les comptes, et
  `writePurchase` laisse alors H/I vides plutôt que d'inventer.
- **Finances** — **reste à investir** (Σ budgets mensuels − dépensé depuis l'ancre), the
  monthly budgets, the CFB inventory value per seller, the cookie status and the
  catalog / price-guide freshness — plus the buttons that ask the PC to run a job.

### « Je suis au sac N et j'ai cette pièce en main »

While sorting, you meet a part that isn't in that bag's list. Tap the **chevron** beside the
bag chip: the leftovers (the `Inventory*.bsx`) open right there, at their **physical**
quantity (×N, the whole purchase). **Calculer** answers for *that* bag, and the green
**Fait** puts the lot **in the bag you are sorting** (see below).

The answer depends on where you are, because a part can only go straight to its Sac once
nothing more of it is coming. Each box is named with its **number**, so you know which of the
three to open:

| ce que l'appli répond | quand |
|---|---|
| **➜ Sac 06** | plus rien de cette pièce ne s'en vient — place-la |
| **⏸ boîte C02** | la même couleur revient dans un sac plus loin (l'appli dit lequel) |
| **⏸ boîte D03 (Red, Blue)** | des couleurs sœurs du même moule arrivent encore |

The number is the one the plan assigned that colour (C) or that mould (D). A leftover the
plan never had to stage has none yet — it gets one the moment **Fait** places it, and the
PC answers with the remark that names it.

**The whole layout is pinned once the remarks exist — Sacs AND box numbers.** A box number is
allocated during the walk from a capacity that depends on how many lots consolidate in the
*whole* set, so adding one lot used to renumber boxes everywhere, including in bags already
sorted: a small bag physically sitting in box `C02` while the new plan started calling it
`C01`, and later bags pointing at the wrong box. `plan.existing_labels` reads the C and D
numbers off the remarks and `_Bins.take` reserves them, so every already-written label stays
true. Measured on 11503, that took the box renumbering in already-sorted bags from 6 rows to 0.

**A colour already sent to its Sac is done.** If an earlier bag's row is terminal (`… -> Sac
04`) and more of that colour turns up later, the new pieces just join `Sac 04` — no C box.
Staging it after the fact would tell the sorter to park pieces that are already bagged and to
empty a box that was never started. (The test is the *earliest* terminal bag vs the key's last
bag, so it keeps holding once the new occurrence has been written with its Sac too.)

**What can still change in a finished bag, and what says so.** One case survives: a colour
parked in box `D01` at bag 3 (it was finished there, waiting for sister colours) is no longer
finished once you add more of it at bag 8, so its bag-3 row becomes a C row while the pieces
are still physically in D01. Nothing in the file reveals that, so it is reported instead:
`restants._earlier_changes` counts the labels that moved in bags before the one being sorted
and the phone says *« ⚠ N remarque(s) ont changé dans des sacs déjà triés (3) — rouvre ces
fichiers »*. That matters most when two people sort one set: the other one must reopen those
files, and if they have the file open in BrickStore and save it, they clobber the remarks —
which does **not** self-repair here, because auto-apply skips a set whose sorting has started.

**The Sac layout is pinned once the remarks exist.** Placing a leftover adds weight, and
flowing the Sacs again from the weights would renumber lots the sorter has **already
bagged** — measured on the real sets, adding one uncatalogued leftover moved up to 41 of 217
lots, 34 of them already sorted. So every re-plan that happens *during* sorting
(`build_plan(…, pin_sacs=True)`) keeps the `Sac NN` already written on each lot and only
places the new one: it joins its mould's Sac if a sister colour has one (a mould is always
packed in one Sac), else the Sac of its nearest neighbour in the plan's sort order — the same
rule the phone predicts with, so what it tells you is what gets written. The cost is that the
Sac can end up over target, which is simply true: a piece added to a full bag makes it
heavier. A fresh **Apply** still plans from the weights; auto-apply already refuses to touch a
set once sorting has begun (`restants_done` → *never renumber the Sacs*).

A lot appended to a bag file carries its **category**: BrickStore fills `<CategoryName>` in
from its own catalog when it opens a file, so anything written before that has none — and
since Phase A sorts on the category, a blank one sorted the lot ahead of everything and packed
it into the first Sac instead of beside its own kind. The planner also backfills a missing
category from another lot of the same mould, so files already written that way still sort right.

**« Fait » places the lot, it does not delete it.** The pieces are in your hand, so they
belong to the bag you are sorting: `restants.place_in_bag` takes the lot off the
`Inventory*.bsx`, adds it to that bag's file, re-plans the set and rewrites the remarks —
then reports the remark the lot now carries (`Sac 06`, `C02 `, `C02 conso -> D03`, …), which
is what tells you where it goes. Removing it from the inventory alone would **lose** the
pieces: no other file of the set holds them, so the CFB master would ship short. The move is
done in **physical** pieces and converted per file, because the bags are already ×N while the
leftovers may still be at base; it is refused if the piece count would change, and a partial
take is snapped to whole copies.

A numbered bag can be several files (`1`, `1 - 1`, `1 - 2 (petit)`), so "au bout du sac N"
means past the **last** file of that bag — that is the position the verdict is computed at.

The search box covers the whole set, for the case the part *isn't* in the leftovers. Hits are
one compact line each; tap one for the verdict:

- **catalogued further on** → *« Mets-la dans 3.bsx »*, **or** a quantity box + « ➜ La traiter
  ici » (prefilled with everything still in later bags) — the part
  is already in your hand, so `restants.transfer_to_bag` moves the quantity out of that later
  bag into the one you are sorting, re-plans the set and rewrites the remarks (the C / D
  lifecycle of that colour and its mould depends on which bags hold it). Only LATER bags are
  drained: an earlier one is already sorted, and taking from it would contradict work done;
- **already been through** → place it by eye, it's in its Sac or a box;
- **⚠ no longer in any file** → a lot you deleted from a bag (it wasn't physically there) and
  that has now turned up. The search still finds it, because the helper also lists the parts
  the **catalogue** says belong to the set that no file holds any more (`absent: true`, from
  `restants._catalog_parts`). A quantity box + « ➜ L'ajouter ici » hands it to
  `restants.add_from_catalog`, which recreates the lot in the bag you are on, re-plans and
  rewrites the remarks. This is the one move that *increases* what the files hold, so it is
  bounded by the catalogue: the set can never hold more of a part than BrickStore says it
  contains (`expected base × N`), and a second attempt is refused. It also fills the lot's
  **category** from `sortpack/categories.py` — harvested from the `.bsx` files we already own,
  because the catalog blob reader doesn't decode categories and Phase A sorts on them.

  This is what makes « supprimer le lot dans BrickStore » a safe move rather than a one-way
  door: before it existed, a deleted lot was invisible everywhere (not in the leftovers, not
  in the search) and both add paths refused it.

The panel then waits for the PC and shows what it decided — the destination Sac, or the box
to park it in — and takes the refreshed list. Rows carry BrickLink's own 3–8 KB thumbnail
(`img.bricklink.com/ItemImage/<type>T/<colour>/<id>.t1.png`), so there is nothing to host and
nothing extra in the JSON: the id, colour and item type are already in the payload.

A partial move is snapped to a whole number of **copies** (multiples of ×N): you own N sets,
so a lot is "so many per copy", and moving 13 of 27 would leave both files holding a fraction
of a copy that nothing downstream could reason about. The move is refused outright if it would
change any total — quantities are re-counted before and after.

The data comes from `restants_<set>.json`, written by the PC at Apply (`restants.bag_help`)
and refreshed whenever the inventory changes — so the phone answers instantly, with no
round-trip. Only **Fait** needs the PC: it queues a `restant_done` (with the bag you are
on) that the poller applies through `restants.place_in_bag`, and the panel then shows the
remark that came back.

**Quantities are physical.** The Inventory file is created at base (one copy) and
`verify.normalize_inventory` brings it to ×N at Apply, because `finalize` folds the inventory
into the CFB master as-is — a base-scale lot would ship at 1/N.

### Un seul poste à la fois (verrou du dossier partagé)

The CFB folder is on Google Drive, so every machine with the project installed sees the same
`.bsx` files, queue and tracking files. Two pollers means both process the queue and both
auto-apply, and whichever Drive sync lands last wins — that is what re-applied three finished
sets on 2026-09-23 at 12:11 from a second machine holding a stale copy of the folder.

`sortpack/lock.py` keeps one owner: `poller_owner.json` in the CFB folder carries the owner's
machine name and a heartbeat (re-stamped every `POLLER_HEARTBEAT_SECONDS`), and every other
machine stands down — no queue, no auto-apply, no writes. A lock left cold for
`POLLER_LOCK_STALE_MINUTES` (laptop closed, machine retired) can be taken over, so the work
never stalls for good.

```
python -m sortpack.lock              # qui tient le verrou
python -m sortpack.lock --take       # le prendre pour ce poste
python -m sortpack.lock --release    # le rendre
```

The GUI's **📱 Demandes** button claims it too (asking this PC to do the work *is* asking it
to own it), and offers to take it when another machine holds it. Set `POLLER_FORCE_OWNER` on
the PC that should always win; `POLLER_LOCK = False` restores the old free-for-all.

It is a **courtesy lock, not a mutex**: Drive takes seconds to minutes to propagate a write,
so two machines starting at the same instant can still overlap. It is sized for the real
problem — a forgotten install quietly fighting the main PC for days — and nothing destructive
rests on it alone (the `MLJQ-xN` marker still prevents a double multiplication, the
verification still fixes quantities, every write is still backed up). **A machine running an
older build ignores the lock entirely**, so it only takes effect once each machine has synced
this code and restarted its GUI / scheduled task.

### What the phone can ask the PC to run

The heavy work needs the local catalog, the price guide and the sheet token, so the phone
only *queues* it (`sort_commands.json`) and the PC executes — from the running GUI (poll,
every `REMOTE_POLL_SECONDS`), the **📱 Demandes** button, or `python -m sortpack.remote`
from Task Scheduler. Each request carries a status the phone reads back
(pending → running → done/error).

| Bouton (Finances)        | Action            | Same as the desktop button |
|--------------------------|-------------------|----------------------------|
| Synchroniser les achats  | `sync_purchases`  | Journal → base + dossiers CFB |
| Rapport d'achat          | `buy_report`      | `job.run(post=True)` — scrape, rank, POST, courriel |
| Inventaire CFB           | `cfb_inventory`   | `cfb_inventory.capture(post=True)` |
| Fin de mois              | `end_of_month`    | recalcule les budgets ET les poste au Sommaire mensuel |
| Vérifier les cookies     | `check_cookies`   | re-probe both CFB sessions |

Writing the remarks needs no request at all: the PC **auto-applies** a set as soon as it is
done cataloguing (Inventory ≤ `CATALOGUE_DONE_FRACTION`, i.e. 5% — that set is finished).
It runs whether or not BrickStore is open, because the decision reads the **files**, not a
flag: each set's bags carry a `<lots>/<stamped>` fingerprint that `auto_apply.json` remembers.

- bags already remarked on every lot → **skipped**, no catalog load, no backup, no rewrite
  (that covers the sets applied from the desktop button, which never touches the tracking file);
- fingerprint changed since we wrote — re-catalogued, or BrickStore saving an old tab over our
  remarks — → **written again**, so a clobbered set repairs itself on the next pass;
- leftovers Inventory already through **Calculer les restants** → **never touched again**: the
  sorting is physically under way, so the Sac layout must not be recomputed.

A set that belongs to an **envoi** is skipped by that pass on purpose — planning it alone
would give it a Sac numbering of its own. Its envoi has its own pass instead
(`batch.auto_apply_all`, `BATCH_AUTO_APPLY`), on the same fingerprint idea but across all the
envoi's sets at once: see *Annoter un envoi pendant le cataloguage* below.

Pasting **fresh** cookies stays a desktop job (*Nouveaux cookies*) — no session cookie ever
travels through Drive; the phone only shows CA/US status and can ask for a re-check.

`sortpack/mobile.py` builds `dashboard.json` (`python -m sortpack.mobile --print` dumps it).
It is rewritten after every processed command, after each desktop accounting button, and by
the GUI poll at most every `MOBILE_DASHBOARD_REFRESH_SECONDS` — so the Finances figures are
as fresh as the PC's last pass, and the tab shows that timestamp. A purchase logged from the
phone lands in the Journal immediately, but moves *reste à investir* only once
**Synchroniser les achats** has run.

## Terminer un set depuis le téléphone

**Construire CFB** on the phone now finishes the set, the way the desktop's
« Traiter le set ? » does (`CFB_PHONE_AUTO_PROCESS`): once the master has actually been
**emailed**, the copies leave stock and the folder goes to the Recycle Bin. It used to build
and mail and stop, so a set finished from the phone stayed in stock and kept its folder with
nothing saying so — which is what happened to 77256.

The order is deliberate, because the two halves carry very different risk:

1. `history.send_to_cfb` — idempotent (*0 if the set has no stock, nothing written*) and
   reversible by deleting the `sent` row, so a re-send cannot double-count and a failure here
   leaves every file untouched;
2. `trash.send_to_recycle_bin` — last, because it is the only step with no undo beyond the
   bin. `sortpack/trash.py` never falls back to a hard delete: if the shell call fails it
   raises and the folder stays.

Two things stop it cold: **the mail did not go out** (a build alone changes nothing), and
**the pre-send check found a gap it could not correct** — then the folder is the only evidence
left that the master is wrong, so it is kept and the phone says so.

`remote._prune_phone_files` then drops what outlives a folder that is gone: the set's
`restants_<set>.json` and its rows in `sort_index.json`, `set_status.json` and
`auto_apply.json`. The poller does not rebuild the index (the desktop does), so without this
the phone would keep listing a set with nothing behind it. The desktop path calls it too, so
both routes leave the same clean state.

## Les batchs — plusieurs sets en un seul envoi

CFB range son inventaire **par passes** : il longe ses bacs dans l'ordre du catalogue en
vidant les sacs. Deux envois séparés, c'est deux parcours complets. D'où la demande de lots
de ~20 000 pièces (≈ 4 sets) en **une** liste et **une** séquence de sacs.

Dans le GUI, la barre **« Batch — plusieurs sets en un seul envoi »** fait tout : une liste
déroulante des batchs, **Nouveau** / **+ set** / **− set** (le set sélectionné dans la liste
du haut), puis **Aperçu**, **Appliquer le batch**, **Consolidation** et **Construire le CFB
du batch**. La pastille de droite indique où en est le remplissage (`3 set(s) · 15 240 /
20 000 pièces`, puis `PRÊT`), et un set déjà réservé à un envoi groupé est marqué dans la
liste des sets (`Trier (0/9)  [envoi-01]`) — pour qu'on ne le traite pas tout seul par
mégarde.

En ligne de commande :

```
python -m sortpack.batch create envoi-01 43011 43027 76342 77256
python -m sortpack.batch status envoi-01      # 20 446 / 20 000 pièces -> PRÊT
python -m sortpack.batch plan envoi-01        # 42 fichiers, 715 lots -> 24 Sacs
python -m sortpack.batch apply envoi-01       # écrit les remarques dans les 4 sets
python -m sortpack.batch cfb envoi-01         # un seul envoi-01.bsx
```

### L'arborescence

Un batch possède un dossier, et ses sets y vivent :

```
CFB/
  Batchs/
    1/
      1 - consolidation.bsx      le fichier de la passe de consolidation
      1.bsx                      le maître, une fois construit
      11503 - Flower Wall/       les listes + l'Inventory du set
      43011 - .../
  77256 - Un set hors batch/     inchangé, traité seul comme avant
  backups/
```

**Un set rejoint un batch en y étant déplacé** : c'est le déplacement, pas une ligne dans
`batches.json`, qui fait foi — on retrouve toujours un set en balayant, même registre perdu.
`− set` le ramène à la racine, où il redevient traitable seul.

`plan.iter_set_folders()` est le seul endroit qui sait où vivent les sets (racine + dossiers
de batch) ; la liste du GUI, l'auto-apply, `verify --all`, `run.py --all` et l'aide au tri du
téléphone passent tous par lui. Le conteneur s'appelle `Batchs` et non `1` : `is_set_folder()`
reconnaît un set à son nom qui commence par un chiffre, donc un dossier de batch à la racine
aurait été pris pour un set.

Deux pièges rencontrés en chemin, corrigés :

- **`shutil.move` n'est pas sûr ici.** Quand le renommage échoue — Google Drive ou
  l'explorateur tient le dossier — il retombe sur copier-puis-supprimer, et si la suppression
  échoue à son tour le contenu se retrouve des deux côtés. `batch._rename` n'utilise plus que
  `os.rename`, atomique sur un même volume : ou ça passe, ou rien ne bouge. Il réessaie
  quelques fois (le verrou de Drive est passager) puis rend la main en disant quoi fermer.
- **Une coquille vide n'est pas un set.** Un dossier que Windows a refusé de supprimer après
  un déplacement porte encore un nom qui commence par un chiffre. `iter_set_folders` exige
  donc au moins un `.bsx` à l'intérieur, sinon la coquille apparaîtrait dans la liste et
  partirait en vérification.

Et puisqu'un set n'est plus forcément sous la racine, le GUI ne peut plus reconstruire son
chemin à partir de son nom : `build_index` porte le chemin, `selected_folder()` et
`status_for()` s'en servent.

**Un batch se déclare avant le tri.** On attend que ses sets soient catalogués, puis Phase A
tourne sur leur **union** : même tri, mêmes 650 g, même cycle C/D, mais une seule numérotation
de Sacs sur tout l'envoi. Il n'y a jamais deux plans, donc rien à fusionner ensuite.

C'est ce qui rend l'approche rentable. Trier set par set puis réunir après coup coûte ~372
manipulations par batch (mesuré sur 43011 + 43027 + 76342 + 77256) ; en planifiant d'avance,
zéro. `build_plan` accepte donc un dossier **ou une liste** de dossiers, et chaque set garde
son propre ×N — ce sont des achats différents, d'où le facteur suivi par FICHIER (`path_mult`)
et non par plan.

### Ce que ça change pendant le tri

- les **Sacs du batch restent ouverts** (24 pour l'exemple) : leur nombre est connu d'avance,
  et une couleur terminée y tombe directement, sans recherche ;
- les boîtes **C** restent nécessaires — elles rassemblent une couleur croisée dans plusieurs
  fichiers, faute de quoi on créerait deux lots de la même (pièce, couleur). Avec un batch
  elles sont plus sollicitées : 168 couleurs à ramasser, pic de 80 petits sacs. D'où
  `MAX_CONSOLIDATION_BOXES` à monter (6 boîtes ≈ 13 petits sacs chacune au lieu de 27) ;
- les boîtes **D disparaissent**. Phase A place déjà toutes les couleurs d'un moule dans le
  MÊME Sac, donc elles s'y retrouvent d'elles-mêmes. Garder D obligeait à retrouver le bon
  sac parmi 108 à chaque couleur finie, pour exactement le même nombre de gestes.

### Quand l'envoi est-il complet ?

`batch.status` répond, et il compte **ce que l'envoi SERA**, pas seulement ce qui est sur la
table :

```
pieces          = pieces_here + pieces_expected
pieces_here     = les sets qu'on a COMMENCÉ à cataloguer (ils ont des sacs numérotés),
                  à leur échelle ×N
pieces_expected = tout le reste : set inscrit sans dossier (catalogue × exemplaires achetés,
                  table `inventory`), ou dossier créé mais encore vide de sacs
full            = pieces >= BATCH_TARGET_PIECES
```

**Un dossier n'est pas une livraison.** `purchases.sync` crée le dossier d'un set et son
fichier d'inventaire *dès l'achat*, à partir du catalogue — le set peut être encore chez le
transporteur. Compter ce dossier comme présent faisait dire à l'outil « 24 893 pièces
physiquement là » alors que 16 730 d'entre elles étaient en camion. Ce qui prouve qu'un set
est arrivé, c'est qu'on ait commencé à le cataloguer, donc des **sacs numérotés**.

Le total, lui, ne change pas : `full` reste `full`, et la décision de fermer reste la même.
Seul le partage entre « là » et « attendu » devient vrai.

À noter : une livraison **partielle** (4 boîtes sur 10) n'est modélisée nulle part. L'outil
connaît « acheté ×10 » et « catalogué N sacs », rien entre les deux.

C'est le seul moment qui compte : **un envoi se décide à l'achat, pas à la réception**. Sans
ça, un batch acheté au complet réclamerait encore des sets pendant les semaines de transit et
de cataloguage — et le classement du lendemain proposerait de le remplir une deuxième fois.
`_expected_pieces` rend 0 dès qu'il manque une moitié (pas d'achat enregistré, set inconnu du
catalogue) : on ne devine pas.

Exemple mesuré — 11503 sur la table (8 163 pièces) plus 10 Jaguar achetés mais pas reçus
(1 740 × 10 = 17 400) : **25 563 / 20 000, complet**. `open_batch_target` cesse alors de
proposer de le compléter, donc le classement suivant vise un envoi neuf.

Où ça se voit :

- `python -m sortpack.batch list` → `25 563 / 20 000 (dont 17 400 en route : 11381) PRÊT`,
  suivi de la commande à lancer ;
- la pastille du GUI → `2 set(s) · 25 563 / 20 000 pièces (dont 17 400 en route)  PRÊT` ;
- la carte *Achats* du téléphone → le détail « dont 11381 (acheté, pas reçu) » sous le total,
  et un bandeau vert quand l'envoi est complet.

Un set sans dossier **et** sans achat enregistré reste un avertissement (`⚠ ni dossier ni
achat`) : personne ne sait ce qu'il apportera. Un set simplement en transit, non — c'est le
cas normal.

**Fermer reste un geste délibéré.** Rien ne ferme un batch tout seul : fermer veut dire
« plus rien n'entre », et c'est un choix, pas une conséquence arithmétique — on peut vouloir
glisser un dernier set en rabais dans un envoi déjà au-delà de la cible, ou renoncer à remplir
un envoi incomplet. Trois chemins, tous réversibles :

```
python -m sortpack.batch close 1        # CLI (batch.close(nom, False) rouvre)
```

- le bouton **Fermer l'envoi** du GUI, qui affiche le compte, distingue le cas complet du cas
  incomplet dans sa confirmation, et **rouvre** quand l'envoi est déjà fermé ;
- le bouton **Fermer l'envoi N** sur la carte d'envoi du téléphone. Il passe par `closeBatch`
  dans le `.gs`, qui écrit `closed` dans le registre et rien d'autre — comme le PC. Déplacer
  des dossiers ou solder le stock reste « Terminer », un geste de bureau.

**L'état d'un envoi se lit dans le REGISTRE, jamais dans le tableau de bord.** `getDashboard`
relit `batches.json` et corrige `shipment.closed` avant de rendre la main. C'est une leçon
payée : `dashboard.json` n'est réécrit que par le PC, à partir de SA vue du registre, donc un
envoi fermé depuis le téléphone pouvait y rester « ouvert » — et le bouton *Fermer l'envoi*
revenait à chaque rechargement sur un envoi déjà fermé. Le registre, lui, est le fichier que
le téléphone vient d'écrire.

La page combine les deux : `s.closed` (le registre, via getDashboard) **ou** `CLOSED` (ce
qu'on vient de fermer dans cette session, pour l'instant où le tableau de bord n'a pas encore
été réécrit).

### Le plan prévisionnel — trier avant que l'envoi soit complet

Un batch se planifie sur l'**union** de ses sets, donc en principe il faut les avoir tous,
catalogués, avant de trier quoi que ce soit. C'est long : un envoi de 20 000 pièces, c'est
~4 sets à acheter, recevoir et cataloguer, pendant que le premier dort sur la table.

Sauf que l'information est là d'avance. L'inventaire d'un set sort du **catalogue**
(`catalogdb.inventory(set, include_extras=True)`, la référence que `verify` utilise déjà),
les grammes de `weightdb`, et les exemplaires de la table `inventory` — donc du Journal. Rien
de tout ça n'attend le colis. `sortpack/forecast.py` calcule la Phase A là-dessus :

```
python -m sortpack.batch forecast 1              # calcule et range
python -m sortpack.batch forecast 1 --dry-run    # affiche seulement
python -m sortpack.batch forecast 1 --copies 11381=10
```

ou le bouton **Plan prévisionnel** du GUI. Le résultat est rangé dans le dossier du batch
(`plan_previsionnel.json`, une ligne par lot : `{"k": [ItemID, ColorID, Condition], "sac": n}`).
Ensuite `batch.plan()`, `batch.apply()` et l'aide au tri du téléphone s'y accrochent seuls
(`plan_inputs` → `build_plan(pinned_layout=…)`) : le set qu'on a en main reçoit les numéros de
Sacs de l'**envoi complet**, et on peut le trier tout de suite.

Mesuré sur 11503 (×9) avec les trois candidats du classement : seul il fait 8 Sacs numérotés
1–8 ; dans l'envoi prévu (34 Sacs, 32 463 pièces) ses 97 lots se répartissent sur les Sacs 1
à 34 — **97/97 au Sac prévu**, 0 moule éclaté sur deux Sacs.

**Fidélité.** Le plan calculé depuis le catalogue pour un set déjà catalogué donne exactement
celui calculé depuis ses `.bsx` : 97 lots sur 97 au même Sac. Ce qui a coûté cher à obtenir :

- **la catégorie**, sur laquelle Phase A trie (Type → Catégorie → Description → Couleur), ne
  se lisait nulle part pour un set qu'on ne possède pas. Elle est maintenant décodée du blob
  — chunk `CAT `, enregistrements `[id u32][nom utf16] + 4 octets`, résolue comme le reste du
  format : le seul « tail » qui fait tomber les 1218 enregistrements pile sur la fin du chunk.
  Vérifiée contre les catégories que BrickStore écrit lui-même dans nos `.bsx` : **450/463**,
  et les 13 écarts sont tous soit des entités XML (`Food &amp; Drink`), soit une valeur
  **périmée** dans nos vieux fichiers (32803 y est « Slope, Curved » quand le catalogue dit
  « Slope, Curved, Inverted »). D'où l'ordre retenu dans `categories.category_of` : le
  catalogue d'abord, la moisson des `.bsx` en secours ;
- **la moisson des `.bsx` ne voyait plus les sets de batch** (`CFB/Batchs/<nom>/<set>/`, deux
  niveaux plus bas qu'un glob `*/*.bsx`). Silencieux et coûteux : 45 des 97 lots de 11503
  sortaient sans catégorie et 72 d'entre eux atterrissaient au mauvais Sac. `_bsx_files()`
  passe maintenant par `plan.iter_set_folders`, le seul endroit qui sait où vivent les sets.

**Ce qu'il ne calcule pas, et pourquoi.** La **Phase B** — les boîtes C, « garde cette
couleur, elle revient » — est hors de portée. Elle parcourt les **sacs numérotés** du set dans
l'ordre et demande, lot par lot, si la couleur revient plus loin. Or le catalogue donne
l'inventaire *total* d'un set, jamais sa répartition entre les sacs de la boîte : cette
répartition est précisément ce que le cataloguage découvre. Sans elle, aucune des questions de
la Phase B n'a de réponse.

La **passe de consolidation finale**, elle, se calcule d'avance : elle ne demande que quelles
couleurs d'un même moule finissent dans le même Sac, et c'est Phase A qui le décide. Le
rapport l'annonce donc — sur 11503 ×9 + 11381 ×10 : *25 Sacs, 114 sous-sacs sur 23 Sacs, le
pire étant le Sac 01 avec 12*.

**Ce que ça ne garantit pas.** Le plan ne vaut que pour la composition prévue. Un set
substitué, une rupture, un ×N qui change, et la numérotation bouge — donc tout ce qui est déjà
trié devient faux. Deux garde-fous :

- un plan dont la composition a bougé est **refusé**, pas appliqué en douce : `plan_inputs`
  lève en listant les écarts (`forecast.verify_composition`) et dit s'il reste sans risque de
  le recalculer, c'est-à-dire si `remote.sorting_started` dit que rien n'est encore en sac ;
- calculer un plan alors que le tri est engagé **prévient** avant d'écraser quoi que ce soit.

Et la règle qui précède tout : on ne fige un plan **qu'une fois les achats passés**. Un set
sans achat au Journal compte pour ×0 — le rapport le dit et refuse de faire semblant.

#### Appliquer un batch incomplet

`batch apply` marche sur un batch dont il manque des sets dès qu'il a un plan prévisionnel :
les sets présents reçoivent les remarques à la numérotation de l'envoi complet. Mesuré sur
11503 : 234 lots annotés dans 16 fichiers, Sacs 1→34 au lieu de 1→8.

Deux choses à savoir, parce qu'elles décident de l'ordre des gestes :

- **Dès qu'un set du batch est en cours de tri, le plan est épinglé** (`pin_sacs`), et ce qui
  est écrit dans les fichiers l'emporte sur le plan prévisionnel. C'est ce qu'on veut au
  ré-apply — quand les sets suivants arrivent, aucune remarque ne doit contredire des pièces
  déjà en sac — mais ça veut dire qu'un set **déjà remarqué et déjà passé en phase « Trier »
  garde son ancienne numérotation**. Apply le dit alors noir sur blanc (« N lots gardent le
  Sac écrit… ») : si rien n'est physiquement en sac, remets la phase en arrière et
  ré-applique, le plan prévisionnel prendra la main.
- **La Phase B sait maintenant ce qui s'en vient** (`pending_keys`). C'était l'asymétrie du
  plan prévisionnel : Phase A planifiait sur tout l'envoi, Phase B ne voyait que les sacs
  présents. Elle vidait donc sa boîte C au dernier sac du set en main — alors que la même
  pièce, même couleur, arrive dans un set pas encore catalogué.

  `batch.pending_keys` liste les (pièce, couleur) des sets du batch **sans sacs** : on ignore
  dans quel sac de ces sets la couleur arrivera — seul le cataloguage le dira — mais on sait
  qu'elle arrivera, et c'est la seule question que Phase B pose. Ces couleurs reçoivent un
  « dernier sac » hors de portée, donc elles ne finissent jamais dans ce passage et restent en
  C. Les lots déjà partis au Sac (`straight`) sont épargnés : on ne désensache pas.

  Mesuré sur 11503 ×9 pendant que le Jaguar est encore en livraison — 8 lots partagés :

  | | C → Sac (trop tôt) | direct au Sac | gardé en C |
  |---|---|---|---|
  | Phase B aveugle | 7 | 1 | 23 |
  | Phase B informée | 0 | 0 | **31** |

  Le coût est négligeable : 72 → 73 couleurs en boîte, toujours 6 boîtes. La plupart y étaient
  déjà parce qu'elles traversent plusieurs sacs du Flower Wall ; le correctif les empêche
  seulement d'en sortir trop tôt. Quand le set manquant est catalogué à son tour, il a des
  sacs, il sort de `pending_keys`, et Phase B calcule le vrai dernier sac sur l'union — les
  boîtes se vident alors au bon moment, toutes seules.

  Les couleurs sœurs n'ont jamais posé de problème : Phase A les met toutes dans le même Sac
  et le plan prévisionnel les connaît déjà.

### La passe de consolidation finale

Le client veut toutes les couleurs d'un moule dans **un sous-sac**. Une fois le batch trié,
`batch.consolidation_pass(plan)` liste, Sac par Sac, les sous-sacs à confectionner — 129 sur
24 Sacs pour l'exemple, soit ~5 par Sac, le pire étant le Sac 18 avec 14.

Le bouton **Consolidation** (ou `python -m sortpack.batch conso <nom>`) écrit
`<nom> - consolidation.bsx` dans le dossier du batch. Il ne contient **que ce qu'il y a à
regrouper** — les moules présents en plusieurs couleurs dans un même Sac. Un moule d'une
seule couleur n'a rien à consolider, et l'écrire ne ferait que noyer le travail réel : sur
le batch d'exemple, 47 lignes au lieu de 97. Les Sacs sans aucun moule multi-couleurs
n'apparaissent pas du tout.

```
4042   Bright Green   x54   Sac 01 · 4042 (Bright Green, Sand Green)
4042   Sand Green     x18   Sac 01 · 4042 (Bright Green, Sand Green)
48729b Lavender      x261   Sac 01 · 48729b (Lavender, Light Nougat)
48729b Light Nougat  x252   Sac 01 · 48729b (Lavender, Light Nougat)
39262  Bright Green   x27   Sac 02 · 39262 (Bright Green, Bright Light Blue, White)
```

L'étiquette est identique sur toutes les couleurs d'un sous-sac, donc le tri texte les colle
ensemble. Le fichier reçoit **le même GuiState que les listes de sacs**, donc il s'ouvre avec
les mêmes colonnes, déjà trié sur Remarks — rien de neuf à apprendre.

Les lots communs à plusieurs sets du batch sont fusionnés en une ligne — dans le Sac ils ne
forment qu'un seul lot.

Elle est **locale** : les couleurs d'un moule sont déjà dans le même Sac, donc on n'ouvre
jamais qu'un Sac à la fois et on n'y cherche rien d'ailleurs. Vérifié sur l'exemple : 0 moule
réparti sur deux Sacs. La seule exception est un moule **plus lourd qu'un Sac**, que Phase A
scinde volontairement — c'est voulu, et c'est le seul cas où la passe cesse d'être locale.

### Terminer un batch

Une fois le maître envoyé, le bouton **Terminer** (proposé aussi juste après le courriel, ou
`python -m sortpack.batch finish <nom>`) solde l'envoi — c'est le « Traiter le set » du flux
par set, à l'échelle du batch :

1. chaque set sort du stock (`history.send_to_cfb`, idempotent et réversible en supprimant sa
   ligne `sent`) ;
2. chaque **dossier de set** part à la corbeille — récupérable, `trash.py` ne fait jamais de
   suppression définitive ;
3. le batch est fermé, ce qui relâche les garde-fous ci-dessous.

**Le dossier du batch reste**, avec le maître et le fichier de consolidation : c'est la trace
de ce qui a été expédié. Seuls les dossiers de sets disparaissent, comme dans le flux par set.
L'ordre compte : le stock d'abord parce qu'il est réversible et qu'un échec y laisse tous les
fichiers intacts, les dossiers ensuite, seule étape sans retour hors corbeille.

### Un set de batch ne se traite plus jamais seul

Trois portes restaient ouvertes, par lesquelles la numérotation du batch se serait fait
écraser sans un mot :

- **l'auto-apply** l'aurait appliqué par set, donc avec sa propre numérotation de Sacs.
  `_apply_action` renvoie maintenant `skip` dès que `remote.in_open_batch()` est vrai. Avant,
  le set n'était protégé que si le tri avait commencé — un set fraîchement catalogué et
  ajouté à un batch serait passé à travers ;
- **les flux de restants** (`Fait`, `La traiter ici`, `L'ajouter ici`, `compute_restants`)
  replanifiaient le set seul, sans `open_sacs` : retour des boîtes D et numérotation par set.
  Ils passent tous par `restants.plan_for()`, qui replanifie **le batch entier** quand le set
  en fait partie — ce qui est de toute façon nécessaire, puisqu'une couleur peut traverser
  deux sets du batch ;
- **« Construire le CFB »** par set aurait produit une liste partielle. Refusé des deux côtés,
  avec le message qui renvoie vers le bouton du batch.

Hors batch, `plan_for()` se comporte exactement comme `build_plan()` : rien ne change.

### Le flux par set n'a pas bougé

Un set hors batch se planifie, s'applique et se finalise exactement comme avant, et son
maître garde son nom (`43011.bsx`). Un batch porte le sien (`envoi-01.bsx`). `batch.batch_of()`
dit si un set appartient à un batch ouvert, pour que le flux par set sache qu'il ne doit pas
le traiter seul.

## Fusion rétroactive — réunir des sets déjà triés

Le flux normal déclare un batch **avant** le tri, et il n'y a alors rien à fusionner. Quand
des sets ont déjà été triés chacun de leur côté, `sortpack/fuse.py` fait le rattrapage :

```
python -m sortpack.fuse 43011 42680 77256
```

Il calcule la Phase A sur l'inventaire **combiné** — même tri, mêmes 650 g, un moule reste
atomique — puis écrit **un fichier d'instructions par set, dans l'ordre de traitement**.

Chaque ligne dit **d'où sortir le lot et où le mettre** — sans quoi le fichier serait
inutilisable, puisque les pièces sont dispersées dans les Sacs du tri d'origine :

```
43011-02 → Sac 04                                   (tu ouvres ton Sac 02 actuel)
43011-02 → Sac 04 · 3023 (Black, Blue, Bright… +10)  (et tu en fais un sous-sac)
43011-restants → Sac 05                              (c'était un restant, placé à l'œil)
```

Le fichier est **trié par Sac d'origine** : on en ouvre un à la fois et on le vide dans les
nouveaux, qui sont tous étalés. L'emplacement d'origine vient de la sauvegarde la plus
complète du set (`fuse.origin_sacs`), par trois chemins : la remarque terminale du lot, sinon
le Sac de son **moule** (une couleur restée en boîte D part avec ses sœurs, et Phase A
garantit qu'un moule finit dans un seul Sac), sinon la remarque de l'inventaire des restants.
Ce qui reste introuvable est marqué `restants` plutôt que de porter un numéro inventé — 4 lots
sur 457 dans le cas présent.

Les sous-sacs nomment trois couleurs puis le compte du reste (`+10`) : le 3023 en a treize
dans ces trois sets, et tout lister rendait la remarque illisible.

```
Fusion 1 - diviser 43011.bsx     219 lots -> Sacs 01–09, 62 sous-sacs
Fusion 2 - integrer 42680.bsx     89 lots -> Sacs 01–10, 24 sous-sacs
Fusion 3 - integrer 77256.bsx    149 lots -> Sacs 01–10, 50 sous-sacs
```

Le premier dit comment **diviser** le set de base ; les suivants disent dans quel Sac verser
chaque lot. Les Sacs sont donc dimensionnés dès le départ pour le total (5,85 kg → 10 Sacs),
et non pour le premier set.

**Consolidation par MOULE seulement, pas de boîte C.** Les sets sont déjà triés, donc un lot
(pièce + couleur) est déjà entier : il n'y a rien à ramasser à travers les sacs. Ce qui reste
est le regroupement que le client demande — toutes les couleurs d'un moule dans un sous-sac :

```
4490   Yellow   x9    Sac 01
14716  Yellow   x9    Sac 01 · 14716 (Dark Orange, White, Yellow)
3004   Orange  x18    Sac 01 · 3004 (Black, Bright Pink, Medium Azure, Orange)
```

L'étiquette liste les couleurs du moule **dans l'ensemble des sets**, donc le premier fichier
annonce déjà ce qu'apporteront les suivants, et elle est **identique d'un fichier à l'autre** :
le sous-sac ouvert par le premier set est reconnu par les suivants. Les trois fichiers portent
le GuiState des listes, donc ils s'ouvrent triés sur Remarks.

La source est le **maître** de chaque set (`<numéro>.bsx`) : à l'échelle physique, exactement
ce qui a été expédié, et il survit à la mise à la corbeille du dossier.

Vérifié sur 43011 + 42680 + 77256 : aucun lot perdu (4788 / 1665 / 3040 pièces identiques aux
maîtres), 0 moule réparti sur deux Sacs, 0 étiquette divergente entre fichiers, aucun Sac
au-dessus de 650 g.

### Le maître de la fusion — ce qui part chez le client

Les fichiers ci-dessus sont **pour nous** : ils disent d'où sortir chaque lot. Le client, lui,
reçoit UN inventaire, celui de l'envoi entier :

```
python -m sortpack.fuse 43011 42680 77256 --master [--name "..."]
```

C'est le pendant de `finalize.build_cfb_files`, qui ne sert pas ici : il part de dossiers de
sets et de leurs sacs numérotés, or ces sets sont déjà expédiés et leurs dossiers à la
corbeille. La source est donc le **maître de chacun**. Trois différences avec les fichiers
d'instructions :

- **un lot partagé par deux sets devient UNE ligne** — 18 lots ici, dont un présent dans les
  trois : 457 lignes deviennent 438. C'est tout l'intérêt de l'envoi groupé, le client range
  sa pièce une fois ;
- **l'étiquette ne porte que la destination** : `Sac 04 · 3023 (Black, Blue, Bright Light
  Blue +10)`. Le `43011-02 →` est notre parcours, pas le sien ;
- **les lignes sortent dans l'ordre des Sacs**, puis dans l'ordre du catalogue à l'intérieur
  de chacun — l'ordre dans lequel il vide l'envoi.

Sorti et vérifié sur cette fusion : 438 lots, 9 493 pièces, 5,85 kg → Sacs 01–10, 74
sous-sacs ; XML valide, quantités identiques à la somme des trois maîtres, 0 doublon, 0
remarque vide, 0 moule éclaté sur deux Sacs, 0 étiquette divergente, aucun Sac au-dessus de
650 g (le plus lourd : 647 g).

## À faire

### 1. L'achat doit raisonner en batchs

CFB expédie par lots de ~20 000 pièces (voir *Les batchs*), donc **l'achat doit viser le
même plancher** : ce qu'on achète doit composer un envoi complet, pas un assortiment qui
laisse un batch à moitié plein pendant des semaines.

Trois changements dans `sortpack/buylist.py`, **sans toucher aux règles de valeur ni de
prix** — le classement par efficacité du capital, `ROI_FLOOR`, `REALIZATION_RATE`,
`MONTHLY_BUDGET_CAD`, `BUDGET_FLEX` et `STANDOUT_ROI` restent exactement ce qu'ils sont :

- **le nombre d'exemplaires dépend du magasin** : **×10 chez Costco, ×9 chez Amazon**.
  Aujourd'hui `config.TARGET_QTY_PER_SET = 10` est un chiffre unique appliqué partout
  (`rank()` : `qty=copies, lot_cost=cost*copies`). Il faut deux valeurs, choisies d'après
  `Candidate.source` (`"amazon"` / `"costco"`) ;
- **compter les pièces** : pièces d'un achat = pièces du set × exemplaires. Le compte par
  copie sort du catalogue, déjà disponible dans `rank(cat, …)` via
  `cat.inventory(set_no, include_extras=True)` — la même référence que `verify`. À stocker
  sur le `Candidate`, à côté de `qty` et `lot_cost` ;
- **`_select_batch` vise le plancher, et le plancher prime.** Aujourd'hui il remplit par
  budget et s'arrête à `BATCH_SIZE` sets, sans jamais regarder les pièces. Il doit continuer
  jusqu'à atteindre **20 000 pièces** (`config.BATCH_TARGET_PIECES`), dans l'ordre de
  classement existant, **même si le budget est dépassé** : un envoi incomplet immobilise le
  tri et fait attendre le client, alors qu'un dépassement de budget ne fait qu'avancer une
  dépense qu'on aurait faite le mois suivant.

  Conséquences à assumer dans le code :

  - `MONTHLY_BUDGET_CAD`, `BUDGET_FLEX` et `STANDOUT_ROI` cessent d'être des plafonds. Ils
    deviennent **informatifs** : le rapport doit afficher le coût réel du lot et signaler
    de combien on dépasse, plutôt que de tronquer la sélection ;
  - `BATCH_SIZE` (nombre de sets distincts) doit céder de la même façon, sinon il empêcherait
    d'atteindre les 20 000 pièces — c'est le même genre de plafond que le budget ;
  - `ROI_FLOOR` et les règles de valeur, elles, **ne cèdent jamais** : on ne descend pas en
    qualité pour remplir un envoi. Si les candidats qui passent le plancher de rentabilité ne
    suffisent pas à faire 20 000 pièces, le rapport le dit et le batch attend le mois suivant
    plutôt que d'acheter du mauvais.

### 2. Le téléphone connaît les envois, mais n'en lance aucun

L'index lui donne `batch` (le nom de l'envoi, fermé compris), donc il **ne propose plus**
« Construire CFB » sur un set d'envoi : il affiche à la place l'envoi, ce qu'il reste à
finir, et où lancer le maître. Le PC refusait déjà la demande (`remote._do_build_cfb`),
mais après coup — bouton pressé, erreur affichée.

Ce qui reste à faire à la main sur le PC : **Consolidation**, **Construire le CFB**,
**Marquer livré**. Les y amener depuis le téléphone demanderait de nouvelles actions dans
la whitelist du `.gs`, et ces trois gestes-là ne sont pas urgents : on les fait assis.

### 3. Un set ajouté à un batch avant d'être catalogué

Il reste dans le registre tant que son dossier n'existe pas. Dès qu'il apparaît, `settle()`
l'emporte dans le dossier de l'envoi — et si l'inscription précède la création (le cas
normal : `_attach_to_batch` inscrit le set le jour de l'achat), `purchases._folder_for` le
crée **directement dedans**, inventaire compris.

`batch plan` refuse toujours tant qu'un dossier manque — **sauf** si le batch a un *plan
prévisionnel* (voir plus haut) : c'est lui qui tient alors la numérotation, et on planifie ce
qui est arrivé.

## Annoter un envoi pendant le cataloguage

Les Sacs d'un envoi restent **ouverts** sur la table du début à la fin. Il n'y a donc aucune
raison d'attendre qu'un set soit entièrement catalogué pour vider ses premiers sacs dedans :
la place est là, les numéros sont fixés, et le reste du cataloguage ne peut qu'ajouter des
pièces dans ces mêmes Sacs. Un envoi s'annote donc **au fur et à mesure**, sac par sac.

Ce que ça demande, et où c'est écrit :

| Ce qu'il faut tenir | Où |
|---|---|
| Un envoi **fermé** se réannote dès qu'un sac apparaît ou perd ses remarques | `batch.auto_apply_all` / `auto_apply_action` — empreinte `11381:12/12:C|11503:97/97:F` (lots annotés / lots, puis cataloguage Fini ou en Cours) sur tous les sacs de l'envoi, rangée dans `batch_apply.json`. Tourne dans la passe de fond (`remote.auto_apply_ready`), sous le même verrou et avec le même catalogue déjà chargé |
| On n'écrit pas **par-dessus une saisie en cours** | `BATCH_APPLY_QUIET_SECONDS` (120 s). « Ce sac est-il fini ? » n'a pas de réponse dans les fichiers : on saisit lot par lot et BrickStore enregistre quand on le lui demande. Le seul signal honnête est « plus personne n'y a touché depuis un moment ». La passe rend `"wait"` sans rien dire (elle repasse toutes les 25 s, un mot par tour noierait le journal) ; le bouton **Appliquer le batch** n'en tient aucun compte |
| **Rien n'est multiplié tant qu'un set n'est pas fini d'être catalogué** | `batch.apply` passe `multiplier=1` pour un dossier dont `_catalogue_done` est faux. Les Sacs sont les mêmes de toute façon : `build_plan` raisonne en quantités **physiques** quoi qu'il arrive (`file_mult` compense l'échelle fichier par fichier), donc un sac annoté à la quantité de base porte exactement les mêmes numéros qu'après multiplication. Ce qu'on évite est le seul état vraiment dangereux, le fichier **mélangé** : le poll tombe forcément un jour au milieu d'un sac en cours de saisie, et s'il le multipliait, les lots ajoutés ensuite resteraient à ×1 dans un fichier tamponné ×N — personne ne les remonterait jamais. Le ×N se fait en une seule passe, à la fin, sacs **et** inventaire ensemble : exactement le comportement éprouvé d'avant, juste précédé des remarques |
| Le ×N **arrive forcément**, même si le dernier geste ne touche aucun sac | C'est pour ça que le `:C` / `:F` est dans l'empreinte. Terminer un cataloguage en **supprimant** les derniers lots de l'inventaire (au lieu de les verser dans un sac) ne change aucune couverture : sans ce drapeau, l'envoi resterait à la quantité de base pour toujours et `finalize` — qui replie les fichiers tels quels — expédierait un maître à 1/N. `batch._needs_scaling` sert de second filet : un set fini mais encore à la base n'est jamais classé « déjà annoté » |
| L'inventaire de restes **ne passe pas à ×N** tant qu'on catalogue | `verify.normalize_inventory` — pendant le cataloguage ce fichier est le **plan de travail** : on en sort les pièces d'UN exemplaire, à la main. Le mettre à ×10 ferait compter dix fois chaque lot. `finalize` passe outre (`force=True`) : le maître doit partir à l'échelle physique |
| Les boîtes **C** ne se vident pas trop tôt | `batch.pending_keys` — pour un set en cours, ce qui reste à venir est exactement son fichier d'inventaire, qu'on lit tel quel. Sans ça, dès qu'un set avait UN sac il passait pour complet (mesuré sur 11503 + Jaguar : 9 lots seraient partis trop tôt) |
| Le téléphone ne propose pas de placer des pièces encore en sachet | `restants.bag_help` — pendant le cataloguage la liste de **restants** est vide (ce ne sont pas des restes, c'est ce qui reste à cataloguer), et ces pièces ne ressortent pas non plus en « absentes » |
| La pastille **N** du téléphone vise le bon sac | `restants.bag_help` renvoie `bag_index` (pastille → indice dans la marche de l'envoi) et `bag_owner`. Le plan d'un envoi couvre plusieurs sets, et chacun a son « 1 » : la page cherchait la dernière étiquette commençant par N et tombait sur le sac de l'autre set |

Conséquence visible : pendant le cataloguage, **les fichiers de sacs affichent la quantité
d'un exemplaire** dans BrickStore, pas la quantité physique. Les remarques, elles, sont déjà
les bonnes — et l'aide au tri du téléphone (`restants_<set>.json`) donne bien les quantités
PHYSIQUES, parce qu'elle les tient du plan et non des fichiers. C'est le compromis assumé :
un affichage à ×1 pendant quelques heures contre l'impossibilité de fabriquer un fichier
mélangé, qui lui partirait faux chez le client.

### Les deux attentes d'un set d'envoi

Elles encadrent le tri, et ne veulent pas du tout dire la même chose :

* **En attente de fermeture du batch** (`STATUS_ATTENTE`, écrite dans `set_status.json`) —
  *avant* : l'envoi peut encore accueillir un set, donc la numérotation des Sacs n'est pas
  figée et on ne trie pas. `settle_phases` y renvoie tout set poussé à « Trier » trop tôt ;
* **En attente du reste du batch** (`STATUS_ATTENTE_RESTE`, **déduite**, jamais écrite) —
  *après* : ce set est fini (catalogué, tous ses sacs cochés) mais un autre set de l'envoi
  ne l'est pas. Il n'y a plus rien à y faire et il ne part pas pour autant, puisque le
  maître couvre tout l'envoi. Sans elle la liste affichait « Trié ✓ », qui se lit « prêt à
  expédier » et pousse à construire un CFB par set.

La seconde est dérivée à l'affichage (`gui.status_for` → `batch.rest_pending`, et la même
règle dans `statusOf` côté téléphone) précisément pour ne rien écrire : `set_status.json`
garde « Trier » + `bags_done`, dont dépendent `sorting_started` et l'épinglage du plan.

Côté **phases**, un set d'envoi fermé dont des sacs portent déjà des remarques est triable :
`gui.status_for` affiche *Trier (n/m) · cataloguage en cours* au lieu de *Cataloguer*, et
l'index du téléphone (`base_status`) dit *Ouverture des sacs* avec un drapeau `cataloguing`
que la page signale. Il ne passe jamais à **Trié ✓** tant que la boîte n'est pas finie — la
coche verte voudrait dire « plus rien à faire ». La condition exacte est
`batch.sortable_while_cataloguing` : envoi **fermé** (numérotation figée, c'est déjà la règle
de `settle_phases`) **et** au moins un sac annoté.

### Fermé n'est pas soldé

`close()` veut dire « plus rien n'entre » ; l'envoi possède encore ses sets, et c'est
justement là qu'on le trie. `finish()` le **solde** et pose `finished` : stock retiré,
dossiers à la corbeille. La nuance compte parce qu'un set se rachète — sans elle, le dossier
d'un rachat irait naître dans l'envoi parti il y a six mois. `batch.owning_batch` (fermé
compris, soldé exclu) est la réponse partout où la question est « où vit son dossier ? » ou
« qui a le droit de le planifier ? » ; `batch_of` (ouvert seulement) reste pour « cet envoi
peut-il encore changer de composition ? ».

## Fin de mois — ce que « Fin de mois » écrit

La chaîne : tu entres la **Fortune** du mois dans la feuille financière, elle en calcule le
**Delta**, et `job.refresh_budget` range le budget du mois **suivant** = delta × taux MLJQ.

`post_budgets` écrit ensuite chaque montant dans **Sommaire mensuel, colonne G**, sur la ligne
du mois qui l'a **gagné** (son delta), pas celui où il se dépense : le budget de septembre
s'affiche sur la ligne d'août. Idempotent par le méta `budget_posted_<mois>` — un mois n'est
réécrit que si son montant a changé, ce qui arrive quand la Fortune est corrigée.

**Le bouton ne postait pas.** `gui.end_of_month` et le job du téléphone appelaient
`refresh_budget` sans `post`, donc seul le rapport d'achat quotidien écrivait dans la feuille :
on entrait sa Fortune, on cliquait « Fin de mois », et la colonne G restait vide là où on
l'attendait. Les deux passent maintenant `post=True`, et la logique d'écriture est sortie de
`job.run` dans `post_budgets` pour qu'il n'y en ait qu'une.

## La fenêtre principale

```
(img) | Set                    | Acheté     | Prochaine action | Batch
(img) | 11503 - Flower Wall    | 2026-09-19 | Trier (0/9)      | (1)
(img) | 11381 - Jaguar E-Type  | 2026-09-30 | Cataloguer       | (1)
```

La liste est triée par **date d'achat croissante** — l'ordre dans lequel les sets arrivent,
donc celui dans lequel on les traite. Un set sans achat enregistré (stock migré) passe en
dernier : faute de date, il n'a rien à dire sur sa place dans la file. La colonne `#0` ne
porte que la vignette (elle vient toujours en premier dans un Treeview) ; la date a sa propre
colonne, à droite du « numéro - nom », lui-même ramené de 600 à 300 px. À l'ouverture,
**la première ligne est sélectionnée** : le set qui attend depuis le plus longtemps.

Les gestes de batch tenaient onze boutons sur la fenêtre alors qu'on s'en sert une fois par
envoi. Ils vivent maintenant dans **« Gérer les batchs… »** — une fenêtre qui liste les envois
(sets, pièces, état) et porte Nouveau / ± set / Plan prévisionnel / Aperçu / Appliquer /
Consolidation / Construire le CFB / Fermer l'envoi / Marquer livré. Chaque bouton réutilise la
méthode qui existait déjà : elles lisaient toutes `self.batch_var`, il suffit de la poser sur
la ligne choisie avant d'appeler.

Un seul geste reste sur la fenêtre principale : **« Appliquer le batch du set sélectionné »**,
parce que celui-là se refait à chaque fois qu'un set avance. Il déduit l'envoi de la sélection
— on vient de cliquer sur le set, le batch n'est pas une question — et accepte un envoi déjà
fermé, pour pouvoir réécrire les remarques d'un lot bouclé mais pas encore livré.

## Le suivi des livraisons

Un set acheté n'est pas un set reçu. `purchases.sync` lui crée pourtant un dossier et un
inventaire dès l'achat, ce qui le faisait apparaître « Cataloguer » alors que la boîte était
encore chez le transporteur. Il naît donc en **« En attente de livraison »**, et en sort tout
seul au courriel de livraison.

### Deux courriels, aucun suffisant

```
Amazon    « Expédié : 6 « LEGO Icons Jaguar E-Type 11381 » »
          → dit QUOI et COMBIEN, jamais que c'est arrivé
Intelcom  « Hourra! Votre colis est arrivé! »  (suivi INTLCMJ081180101)
          → dit QUE c'est arrivé, jamais QUOI
```

Le numéro de suivi Intelcom n'apparaît dans aucun courriel d'Amazon : rien ne relie les deux.
L'appariement est donc une **inférence assumée** — une livraison constatée solde l'expédition
en attente la plus ancienne (FIFO). Juste tant que les colis arrivent dans l'ordre où ils sont
partis ; faux le jour où deux se croisent.

`appscript/SortPack_deliveries.gs` scrute Gmail (déclencheur horaire) et range
`deliveries.json` ; `sortpack/deliveries.py` apparie, garde la trace dans
`deliveries_matched.json` et fait tomber la phase. Le PC le fait au poll et au rafraîchissement
du GUI.

Le numéro de set est le **dernier** groupe de 4–7 chiffres du sujet : les noms de sets
contiennent volontiers des nombres (*1989 Batmobile 76240*, *Heartlake City 4x4 42680*) et
LEGO met toujours la référence à la fin. Vérifié sur les quatre formes.

### Ce que ça ne peut pas faire

* **Tous les colis ne passent pas par Intelcom.** Un set livré par un autre transporteur ne
  produira jamais de courriel « arrivé » et restera en attente jusqu'à ce qu'on le débloque à
  la main. L'inférence ne peut pas inventer ce qu'elle ne reçoit pas ;
* **Amazon scinde les commandes.** Les 10 Jaguar sont partis en 6 + 4 : deux expéditions pour
  un achat. La première livraison fait passer le set à « Cataloguer » alors qu'une partie est
  encore en route — c'est voulu, on catalogue ce qu'on a ;
* une phase déjà plus avancée n'est jamais reculée : un deuxième colis du même achat ne
  ramène pas en cataloguage un set qu'on est en train de trier.

**Portée Gmail.** Ce fichier LIT la boîte de réception : la première exécution redemandera
l'autorisation du projet Apps Script (`gmail.readonly`). C'est le seul scope ajouté.

## « Séparer les sacs » : la seule étape qui passe AVANT le cataloguage

L'ouverture des sachets n'est plus une étape à part. On répartit d'abord les sachets
numérotés des N boîtes en **piles par numéro**, et on catalogue ensuite pile par pile —
donc l'ouverture fait partie du cataloguage, et ce qui doit absolument le précéder, c'est
la séparation. L'ordre physique n'est pas négociable : sachets ouverts et versés, le
cataloguage par sac devient impossible.

D'où le renversement : **l'ancien « Ouverture des sacs », qui venait après le cataloguage,
est remplacé par « Séparer les sacs », qui vient avant.** Conséquence voulue : il ne reste
que **deux** phases qui se chevauchent, « Cataloguer » et « Trier ».

Elle est la seule phase que rien ne peut déduire. Répartir des sachets ne laisse aucune
trace : pas un `.bsx`, pas une ligne de base. Elle se clôt donc sur un geste — le bouton
**« Séparation terminée »** du téléphone (`finishSeparating`), ou *Marquer « sachets
séparés »* au clic droit sur le PC — qui n'écrit qu'une chose, `separated` dans
`set_status.json`. Il ne choisit aucune phase : la suivante se déduit des fichiers comme
tout le reste. C'est ce qui le distingue de l'ancien bouton, qui devait trancher entre
« Trier » et « En attente de fermeture du batch » — une logique dupliquée en JavaScript,
donc une deuxième vérité à maintenir.

Garde-fou : **un set qui a déjà des sacs numérotés est forcément séparé**, on ne le lui
réclame pas. Sans ça, un set d'avant ce changement — ou un cataloguage commencé sans avoir
cliqué — resterait coincé sur la pastille bleue pour toujours.

## Trier déclenche l'application, il ne la bloque plus

Le piège était là : « Trier » était une phase qu'on posait à la main, et `apply` s'en servait
comme preuve que le tri avait commencé — donc il **épinglait** et refusait d'écrire la
numérotation du plan prévisionnel. On lançait le plan, on appliquait, et rien ne changeait.

La logique est retournée. **C'est l'application qui fait entrer dans le tri**, pas l'inverse.

```
« Je commence à trier »  (clic droit, ou le téléphone)
   envoi pas encore fermé  → En attente de fermeture du batch   (rien n'est écrit)
   envoi fermé             → on APPLIQUE, puis → Trier
```

Un envoi pas encore fermé ne se trie pas : tant qu'un set peut y entrer, la numérotation des
Sacs n'est pas figée, et ensacher reviendrait à suivre des numéros qui vont changer. Le set
attend donc, et **bascule tout seul** en « Trier » — remarques appliquées — dès que l'envoi
est fermé.

`batch.settle_phases` tient les trois règles, et tourne au rafraîchissement du GUI **et** au
poll du PC (donc une phase posée depuis le téléphone est rattrapée au passage suivant) :

1. envoi pas fermé + set en « Trier » → redescend en attente ;
2. envoi fermé + sets en attente → on applique l'envoi, puis ils passent à « Trier » ;
3. envoi fermé + Sacs écrits qui contredisent le plan prévisionnel + **rien en sac** → on
   ré-applique sans épingler. C'est le rattrapage d'un plan figé après coup : le Flower Wall
   est passé de 8 Sacs à 25 par cette règle.

**`_ticked` est le seul veto.** L'épinglage ne suit plus la phase mais la preuve physique —
une liste cochée ou des restants placés. Se fier à la phase reviendrait à refuser d'écrire ce
qu'on est justement en train d'écrire.

Et `plan_inputs` calcule le plan prévisionnel **tout seul** quand un set de l'envoi n'a aucune
liste : exiger un clic avant le premier Apply était un piège de plus. Il refuse en revanche de
deviner quand un set à venir n'a aucun achat enregistré — son ×N vaudrait 0, et un plan calculé
sur un set absent est pire que pas de plan.

## Le cycle de vie d'un set

```
En attente de livraison          (posé À L'ACHAT, levé au courriel de livraison)
  → Séparer les sacs             (geste : « Séparation terminée »)
  → Cataloguer  ⇄  Trier (n/N)   ← les DEUX seules phases qui se chevauchent
  → Trié ✓   /   En attente du reste du batch
  → [Construire le CFB]  → Prêt pour livraison      (posé AUTOMATIQUEMENT)
  → [Marquer livré]      → Livré                    (posé À LA MAIN)
```

« Cataloguer » et « Trier » se chevauchent parce qu'un envoi s'annote sac par sac : les sacs
déjà faits se trient pendant qu'on catalogue les suivants (voir *Annoter un envoi pendant le
cataloguage*). L'étiquette le dit — *Trier (2/5) · cataloguage en cours* — et le set ne
passe jamais à « Trié ✓ » tant que la boîte n'est pas vide.

Cataloguer, Trier et Trié se lisent dans les fichiers et les cases cochées. Les deux derniers
ne se devinent pas, parce qu'ils parlent du monde physique :

- **Prêt pour livraison** est posé par `batch.mark_phase` dès que le maître est construit —
  pour un batch comme pour un set seul. Le set est emballé et n'attend plus que le
  transporteur ; son dossier **reste visible**, on peut encore l'ouvrir. L'état est écrit
  dans `set_status.json` et passe AVANT la lecture des fichiers dans `status_for` : sans ça,
  un set dont l'inventaire a été vidé retomberait sur « Cataloguer ».
- **Livré** est le bouton *Marquer livré* (anciennement *Terminer*). C'est lui qui solde :
  les copies sortent du stock (donc de l'inventaire non traité), les dossiers de sets vont à
  la corbeille, le batch se ferme. Le set quitte alors la liste — son dossier n'existe plus.

### L'onglet Historique

La liste du GUI a deux onglets : **Sets** (ce qui est en cours) et **Historique** (ce qui est
livré), avec la même vignette Brickset de chaque côté. L'Historique ne balaie pas le disque —
au « Livré » il n'y a plus rien à balayer. Il lit la table **`sent`**, la seule trace qui
survit, et retrouve le reste après coup : le nom par le catalogue, l'envoi par le registre
(les batchs fermés y restent).

**Le téléphone a le même onglet**, nourri par `delivered` dans `dashboard.json` (le PC le
tire de la même table). Les sets y sont groupés par date d'envoi, avec leur boîte — on
reconnaît un set à son image bien avant de lire son numéro. Et `getSets` transmet désormais
la `phase`, pour que « Prêt pour livraison » s'affiche là aussi (pastille violette) au lieu
du « Ouverture des sacs » qu'il montrait sur un set déjà emballé.

```
42680 - Heartlake City Convenience Store   livré 2026-09-27   9 copies
43011 - Lionel Messi - Soccer Highlights   livré 2026-09-27   9 copies
75638 - Battle at Arlong Park              livré 2026-08-31  10 copies
```

Les sets livrés avant l'arrivée des batchs n'ont pas de numéro d'envoi : ils n'en ont jamais
eu, et inventer une valeur serait pire que la case vide.

## Inventaire non traité — ce qui est chez nous

La colonne *Inventaire MLJQ* (W des « Résultats mensuels ») ne comptait que ce que le portail
CFB affiche. Un set acheté, trié, emballé mais pas encore saisi par CFB n'était **nulle part** :
plus en encaisse (il est payé), pas encore au portail. Acheter de l'inventaire faisait donc
baisser la Fortune, donc le delta, donc le budget du mois suivant — on se punissait d'investir.

`sortpack/untreated.py` comble le trou : pour chaque set de la table `inventory` (ce qui est
**chez nous**, pas ce qui est déjà parti), part-out sur les prix **vendus des 6 derniers mois**
× `REALIZATION_RATE`, converti en CAD au taux du jour. C'est la valeur de revente attendue
*avant* la commission CFB — elle n'est pas due tant que rien n'est vendu, et c'est la seule
base cohérente avec la colonne V (Binobrick), qui est elle aussi une valeur de marchandise.

```
python -m sortpack.untreated          # le détail, set par set
```

`cfb_inventory` l'**ajoute** à ce qu'il poste pour MLJQ (`"untreated": True` dans
`CFB_INVENTORY_VENDORS`), mais ne le mélange pas à l'instantané rangé en base : celui-ci est
une *lecture du portail* et doit le rester, sinon on ne saurait plus ce que CFB détient
vraiment. Le log affiche les deux et leur somme.

Un set quitte le « non traité » exactement quand il est marqué **Livré** (`send_to_cfb` le
sort de `inventory`) — c'est-à-dire quand il entre dans la file de saisie de CFB. Pas de
double compte, pas de trou.

## Vérification avant l'envoi à CFB

`build_cfb_files` validates the set **before** it builds the master
(`CFB_VERIFY_ON_BUILD`), because this is the last moment before it leaves:

- the leftovers are brought to the bags' **×N** scale. The Inventory is written at base (one
  copy) and finalize folds it in **as-is**, so an un-normalised leftover ships at 1/N — which
  is exactly what happened to set 77256 on 2026-09-26: four lots went out at 1, 1, 1 and 3
  instead of 8, 8, 8 and 24, and a fifth merged short (17 instead of 24), 2991 pieces instead
  of 3040;
- the same reconcile Apply runs then checks the counts.

It used to be Apply's job alone — but **auto-apply skips a set as soon as its sorting has
started** (`restants_done` → *never renumber the Sacs*), so a set can reach the master having
never been through another Apply. Whatever the check does is reported back to the GUI log and
to the phone's result line, not just written to a log nobody reads: the master is already gone
by the time you would look.

## Vérification des quantités (cross-check du cataloguage)

Cataloguing by hand means decrementing the leftovers `Inventory*.bsx` and incrementing a
numbered bag. Miss one side and a piece is lost or counted twice. Before the remarks are
written, `sortpack/verify.py` compares the files against the set's **real** inventory and
puts the difference back.

```
python -m sortpack.verify "<set folder>"          # rapport seulement
python -m sortpack.verify "<set folder>" --fix    # corrige les quantités (sauvegarde d'abord)
python -m sortpack.verify --all [--fix]           # tous les sets
```

Also the GUI button **« Vérifier les quantités »** (on demand, useful *during* cataloguing),
and automatically just before every Apply — desktop button, `run.py --apply` and the headless
auto-apply alike (`VERIFY_ON_APPLY`).

**The reference** is BrickStore's own catalog, `cat.inventory(set, include_extras=True)`, at
base quantity for one copy. Spares are included on purpose: the box physically contains them
and you catalogue them. Verified on the three finished sets — 42680 (89 lots), 43011 (219),
77256 (150) reconcile **exactly**; excluding the spares instead showed 15–35 phantom lots
"over" by +1 each.

**What it holds** = `Σ lots des sacs ÷ N + Σ lots de l'inventaire`, where N is the ×Quantité
each bag is already multiplied by — detected like `build_plan` does (the `MLJQ-xN` marker *or*
every quantity dividing by N, since BrickStore strips the marker when it saves).

**How a gap is closed**, per part:

| situation | action |
|---|---|
| il manque des pièces | ajoutées au lot de l'inventaire s'il existe, sinon à un lot de sac |
| la pièce est absente partout | recréée dans l'inventaire |
| il y a trop de pièces | retirées de l'inventaire d'abord, puis des sacs ; un lot à 0 disparaît |
| lot inconnu du catalogue | supprimé (`VERIFY_DROP_UNKNOWN`) |

A gap beyond `VERIFY_MAX_LOT_FRACTION` (50 % des lots) or `VERIFY_MAX_QTY_FRACTION` (25 % des
pièces) means the *set* is wrong, not the count — the pass reports and writes nothing. An
already-multiplied set is reported but never rewritten. Everything is backed up first.

Real example, set 11503 mid-cataloguing: the inventory lot for *Headgear Crown / Bright Light
Blue* held 14 with `DifferenceBaseValues Qty="21"` — it started at 21 (the catalog figure),
7 went into bag 2 and 14 into bag 6, but only 7 were taken off the inventory. Those 14 were
counted twice; the pass takes them back off the inventory.

## What it does

For each set folder (the numbered bag files, `Inventory*` and empty files skipped):

- **Weight** for every part comes from BrickStore's local catalog
  `database-v12` (verified against the app's Total Weight column). Quantities are
  multiplied by how many copies we own (from the SQLite `inventory` table) at Apply.
- **Phase A – Sac assignment:** every distinct part is placed in a final bag
  (`Sac n`) of ~650 g, after sorting the whole inventory by
  Category → Description → Color. A **multi-colour mould** (same part in ≥2 colours)
  is placed as one atomic block so all its colours land in the **same Sac** — it only
  splits across Sacs if the block alone is heavier than a Sac.
- **Phase B – Consolidation (`C → D → Sac` lifecycle):** walking the bags in order
  (`1, 1-1, 2, 2-1, …`):
  - **C** (lot consolidation) — a colour still being collected across bags is staged in a
    bin `C{k}`.
  - **D** (colour consolidation) — used only when a multi-colour mould's colours **finish
    in different bags**, since a D bag exists to park a colour that is done while its
    sisters are still coming. When they all finish in the same bag — even if some got there
    through a C box — the mould is complete right there and ships straight to its shared Sac
    as a group row; there is no box to open. Once a colour of such a mould is finished, it
    waits in a colour-bag `D{k}` for its sister colours; the bag ships to its Sac once every
    colour is in. D rows also list the mould's colours, e.g. `D03 (Red, Blue)`, so you know
    what to look for in the box. (D sorts **after** C, so lot-consolidation rows come first.)
  - **`+ (…)` — combine these now.** Several colours of one mould often finish in the
    **same** bag; they must end up in **one** bag (in the D box, or straight in their shared
    Sac), so every such row names its siblings: `D01  + (Black, Blue)`, `Sac 02 + (Bright
    Pink, White)`. It cannot be left to identical remarks sorting adjacent, because a colour
    arriving out of a C box reads differently — `C01 conso -> D01 conso (Black, Blue, Red) +
    (Medium Nougat)` beside `D01 conso (Black, Blue, Red)  + (White)`. Don't confuse the two
    lists: `conso (…)` is what the box **already** holds from earlier bags (that's how you
    recognise the bag), `+ (…)` is what arrives **with** this lot.
  - **NEW vs `conso` is per colour (C) / per mould (D).** The pure label `C01` is *this
    colour's first bag* (start its small bag in box C01); `C01 conso` means *this colour
    is already partially in C01* (seen in an earlier bag) — so nothing is ever `conso` in
    its first bag. Box capacity is **dynamic**: each set spreads its consolidating lots over
    at most `MAX_CONSOLIDATION_BOXES` (3) boxes, so a box holds ~⅓ of them and small sets get
    tiny, easy-to-scan boxes. Remarks are a chain read left→right, e.g. `Sac 06`, `C01*`,
    `C01 conso -> Sac 06`, `D03* (Red, Blue)`, `C01 conso -> D03 (Red, Blue)`,
    `D03 conso -> Sac 06 (Red, Blue)`. Numbers are zero-padded so a click-sort on the Remarks
    column groups `C*` then `D*` first, then `Sac` in order.
  - **Multi-step first:** single-step staging rows carry a trailing `*` (`STAGE_MARK`) so the
    multi-step `-> Sac` / `-> D` finalize rows sort **ahead** of the plain `C01*` / `D01*`
    staging rows within each bin group.
  - **Sac divider:** one lot per Sac (per file) carries `Sac 06 -----------` so each Sac
    block ends with a visible separator after the sort.
  - **Column layout:** every written bag gets a known-good `GuiState` (columns + Remarks
    sort) injected, so it opens already displayed and sorted correctly.

**« Appliquer restant » a été retiré** (bouton téléphone et bureau). Les restants se placent
un par un, dans le sac où l'on est, par « Fait » / « L'ajouter ici ». Deux choses en
dépendaient et ont été rebranchées : `remote.sorting_started()` remplace `restants_done()`
comme signal « le tri a commencé » (il lit la phase réelle — un sac coché ou « Trier » — au
lieu d'un effet de bord du bouton), sans quoi l'auto-apply se remettrait à renuméroter les
Sacs en plein tri ; et « Construire CFB » ne dépend plus que du tri terminé, sinon le bouton
serait resté désactivé pour toujours. `compute_restants` reste dans le code, sans appelant.

**Restants — "Calculer les restants" (retiré de l'interface) :** while sorting, mark a lot's Remarks with `x`
when it's missing/uncertain in that bag. This button **moves** those `x` lots out of the
bags into the set's `Inventory*` file and writes where each leftover goes, reusing the
Sacs already in progress (`Sac 06`, or `C -> Sac 06` / `D conso (Green, Red) -> Sac 06` when
it belongs to a consolidation or colour group — the D case lists the mould's colours so you
know what to regroup). Process that inventory last; delete for good anything
still absent. The CFB finaliser then folds the inventory into the master; there is no
`- restants.bsx`.

Backups of every file are copied to a timestamped folder under
`G:\Mon Disque\MLJQ - Sort and Pack Helper\backups` (inside Google Drive) before
anything is written.

## Where to change things

Everything tunable — paths, the 650 g target, C-bin / B-bin capacity, the label
vocabulary and Sac divider — lives in **`sortpack/config.py`**. The algorithm is in
`sortpack/plan.py` (Phase A on its own is `plan.flow_sacs`, shared with the forecast);
catalog weight reading in `sortpack/weightdb.py`; the catalog itself (inventories,
categories) in `sortpack/catalogdb.py`; batches in `sortpack/batch.py` and their forecast
in `sortpack/forecast.py`; `.bsx` read/write in `sortpack/bsx.py`.
