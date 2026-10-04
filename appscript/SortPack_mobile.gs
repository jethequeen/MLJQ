/**
 * MLJQ — Sort & Pack : mobile web-app (Apps Script).
 * A phone page to: see the sets + their status, check off numbered bags as you sort
 * (synced two-way with the desktop app), and log a purchase.
 *
 * It reads three small JSON files the DESKTOP writes into the Google-Drive CFB folder:
 *   sort_index.json  — the set list (name, number, base status, bag count)   [read only]
 *   set_status.json  — the per-bag check-offs {key:{status,bags_done}}        [read + write]
 *   dashboard.json   — the buy list + budget + CFB inventory + cookie status  [read only]
 * so the phone never has to parse any .bsx, open the history DB or read the sheet. A
 * purchase is posted by reusing the EXISTING CFB web-app's `purchase` action (so the
 * Journal logic lives in ONE place).
 *
 * Three tabs: Tri (sets + bag check-offs), Achats (the ranked buy list), Finances (reste à
 * investir + the jobs the PC runs on demand: synchroniser les achats, rapport d'achat,
 * inventaire CFB, fin de mois, revérifier les cookies).
 *
 * ── SETUP ────────────────────────────────────────────────────────────────────
 * 1) CFB_FOLDER_ID is already set to  My Drive ▸ BSX ▸ CFB . Only change it if that folder
 *    moves: open it in a browser and copy the id from its URL (…/folders/<THIS>).
 * 2) WEBAPP_URL / SECRET are already your existing CFB Apps Script deployment + token.
 * 3) Add an HTML file named exactly "page" (the mobile UI) — see SortPack_mobile_page.html.
 * 4) Deploy ▸ New deployment ▸ Web app ▸ Execute as: Me ▸ Who has access: Only myself.
 *    Open the /exec URL on your phone (signed into this Google account) and "Add to Home
 *    Screen". Only-myself keeps it private with no token in the URL.
 * ─────────────────────────────────────────────────────────────────────────────
 */

// 1) Drive folder id of  My Drive/BSX/CFB  (where the desktop writes sort_index.json,
//    set_status.json, sort_commands.json and dashboard.json). Already filled in.
var CFB_FOLDER_ID = '1EQTcgHhjIy9G9O83Jl-zkpicf9_NzpmG';

// 2) Your existing CFB Apps Script web-app (handles the 'purchase' action) + shared token.
var WEBAPP_URL = 'https://script.google.com/macros/s/AKfycbzzmFzyiDScqG5vW05Mi3G5m_lD5ec9afGQJpq68yvNWLmLJFhQ3lAnNpEGRljj0ixN/exec';
var SECRET = 'zxzxws12';

var STATUS_FILE = 'set_status.json';
var INDEX_FILE = 'sort_index.json';
var COMMANDS_FILE = 'sort_commands.json';
var DASHBOARD_FILE = 'dashboard.json';
var BATCH_FILE = 'batches.json';     // le registre des envois (ecrit aussi par le PC)

// Per-set requests (carry a set key) vs the global jobs the Finances tab fires (key '').
var SET_ACTIONS = ['restants', 'build_cfb', 'restant_done', 'restant_move'];
var BAG_HELP_FILE = 'restants_{set}.json';   // per-set, written by the PC at Apply
var GLOBAL_ACTIONS = ['sync_purchases', 'buy_report', 'cfb_inventory', 'end_of_month',
                      'check_cookies'];


function doGet() {
  // Only 'viewport' is universally allowed by addMetaTag; the other web-app meta tags live
  // inline in page.html (some Apps Script versions reject them here).
  return HtmlService.createHtmlOutputFromFile('page')
    .setTitle('Sort & Pack')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, viewport-fit=cover');
}

// ── Drive JSON helpers ───────────────────────────────────────────────────────
function cfbFolder_() { return DriveApp.getFolderById(CFB_FOLDER_ID); }

function readJson_(name) {
  var it = cfbFolder_().getFilesByName(name);
  if (!it.hasNext()) return null;
  try { return JSON.parse(it.next().getBlob().getDataAsString('UTF-8')); }
  catch (e) { return null; }
}

function writeJson_(name, obj) {
  var folder = cfbFolder_();
  var it = folder.getFilesByName(name);
  var s = JSON.stringify(obj, null, 2);
  if (it.hasNext()) it.next().setContent(s);
  else folder.createFile(name, s, 'application/json');
}

// ── API called from the page via google.script.run ───────────────────────────

/** The sets to show: merges the desktop's index with the shared bag check-offs. */
function getSets() {
  var idx = readJson_(INDEX_FILE) || { sets: [] };
  var st = readJson_(STATUS_FILE) || {};
  var cmds = (readJson_(COMMANDS_FILE) || { commands: [] }).commands || [];
  var latest = {};
  cmds.forEach(function (c) {                          // array is chronological → last wins
    if (c.key && (c.action === 'restants' || c.action === 'build_cfb')) latest[c.key] = c;
  });
  var out = (idx.sets || []).map(function (s) {
    var e = st[s.key] || {};
    var bags = Number(s.bag_count || 0);
    var done = (e.bags_done || []).filter(function (b) { return b >= 1 && b <= bags; });
    var row = {
      name: s.name, key: s.key, number: s.number || '',
      base: s.base_status, bags: bags, done: done,
      // Le cataloguage n'est pas fini mais le set est quand meme triable (envoi ferme,
      // Sacs numerotes) : `base` dit « Ouverture des sacs », ceci dit qu'il en reste a
      // cataloguer, pour que la page le signale au lieu de laisser croire au set complet.
      cataloguing: !!s.cataloguing,
      // L'envoi auquel il appartient ("" sinon) : un set d'envoi ne construit jamais son
      // maitre tout seul, c'est tout l'envoi qui part en un fichier.
      batch: s.batch || '',
      batch_open: !!s.batch_open,
      // Sachets repartis par numero ? Purement physique, donc un geste (finishSeparating),
      // ou deja evident si le set a des sacs numerotes (le PC le deduit).
      separated: (s.separated === undefined ? true : !!s.separated),
      // La phase POSEE A LA MAIN (ou par la construction du maitre) : « Prêt pour livraison »
      // ne se devine d'aucun fichier, et sans elle le telephone affiche « Ouverture des sacs »
      // sur un set deja emballe.
      phase: e.status || '',
      sorting: (e.status === 'Trier') || done.length > 0,
      // default has_inventory to TRUE when a (stale) index doesn't carry the field, so
      // restants stays available and build stays gated until restants is actually done.
      has_inventory: (s.has_inventory === undefined ? true : !!s.has_inventory),
      restants_done: !!s.restants_done
    };
    var c = latest[s.key];
    if (c) row.cmd = { action: c.action, status: c.status, result: c.result || '' };
    return row;
  });
  return { generated: idx.generated || '', sets: out };
}

/** Queue a step for the PC to run: a per-set one ('restants' / 'build_cfb') or a global job
 *  ('sync_purchases', 'buy_report', 'cfb_inventory', 'end_of_month', 'check_cookies' — pass
 *  an empty key). Returns {ok,status}. A global job already waiting is not queued twice.
 *  (Writing the remarks is automatic on the PC once cataloguing is done — no command.) */
function queueCommand(key, action, payload) {
  var isGlobal = GLOBAL_ACTIONS.indexOf(action) >= 0;
  if (!isGlobal && SET_ACTIONS.indexOf(action) < 0) return { ok: false, error: 'action invalide' };
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var idx = readJson_(INDEX_FILE) || { sets: [] };
    var meta = (idx.sets || []).filter(function (s) { return s.key === key; })[0] || { key: key };
    var data = readJson_(COMMANDS_FILE) || { commands: [] };
    if (isGlobal) {
      key = '';
      meta = { key: '', number: '', name: '' };
      var waiting = (data.commands || []).filter(function (c) {
        return c.action === action && (c.status === 'pending' || c.status === 'running');
      });
      if (waiting.length) return { ok: false, error: "déjà demandé — le PC ne l'a pas encore fait" };
    }
    var cid = Utilities.getUuid().replace(/-/g, '').slice(0, 12);
    data.commands.push({
      id: cid, key: key, number: meta.number || '', name: meta.name || '',
      action: action, created: new Date().toISOString().slice(0, 19),
      status: 'pending', result: '', finished: '', payload: payload || {}
    });
    if (data.commands.length > 40) data.commands = data.commands.slice(-40);  // cap growth
    writeJson_(COMMANDS_FILE, data);
    return { ok: true, id: cid, status: 'pending' };
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}

/** The Achats + Finances tabs: the desktop's dashboard.json plus the status of each global
 *  job still in the queue, so a button can show ⏳ / ⚙ / ✔ like the per-set ones do. */
function getDashboard() {
  var dash = readJson_(DASHBOARD_FILE);
  var cmds = (readJson_(COMMANDS_FILE) || { commands: [] }).commands || [];
  var jobs = {};
  cmds.forEach(function (c) {                          // chronological → last one wins
    if (GLOBAL_ACTIONS.indexOf(c.action) >= 0) {
      jobs[c.action] = { status: c.status, result: c.result || '', created: c.created || '',
                         finished: c.finished || '' };
    }
  });
  if (!dash) {
    return { ok: false, jobs_queue: jobs, jobs: {},
             error: "dashboard.json introuvable — lance l'appli sur le PC une fois." };
  }
  dash.ok = true;
  dash.jobs_queue = jobs;

  // L'etat d'un envoi se lit dans le REGISTRE, jamais dans le tableau de bord.
  // dashboard.json n'est reecrit que par le PC, a partir de SA vue de batches.json : un envoi
  // ferme depuis le telephone peut donc y rester « ouvert » tant que le PC ne s'est pas remis
  // a jour. Le registre, lui, est le fichier que le telephone vient d'ecrire — il a raison.
  // Sans ca, le bouton « Fermer l'envoi » revient a chaque rechargement sur un envoi deja
  // ferme, et on clique dans le vide.
  var sh = dash.buy_list && dash.buy_list.shipment;
  if (sh && sh.name) {
    var reg = readJson_(BATCH_FILE) || {};
    var b = reg[String(sh.name)];
    sh.closed = !b || !!b.closed;        // disparu du registre = plus un envoi en cours
  }
  return dash;
}


/** The sorting helper for one set: the leftovers with their physical quantities, each
 *  one's destination Sac, and the bags that still hold that part or its sister colours —
 *  enough for the phone to answer « je suis au sac N, où va cette pièce ? » on its own.
 *  Written by the PC at Apply (restants_<set>.json). */
function getRestants(setNumber) {
  var name = BAG_HELP_FILE.replace('{set}', String(setNumber || '').trim());
  var data = readJson_(name);
  if (!data) {
    return { ok: false, error: 'Pas encore de liste pour ce set — elle est créée quand le '
                             + 'PC écrit les remarques.' };
  }
  // the latest leftover command for this set, so the panel can show what the PC did
  var cmds = (readJson_(COMMANDS_FILE) || { commands: [] }).commands || [];
  var last = {};
  cmds.forEach(function (c) {
    if ((c.action === 'restant_move' || c.action === 'restant_done')
        && String(c.number || '') === String(setNumber)) {
      last[c.action] = { status: c.status, result: c.result || '', created: c.created || '',
                         key: (c.payload && c.payload.key) || '' };
    }
  });
  data.ok = true;
  data.cmds = last;
  return data;
}


/** Persist the checked-off bag numbers for one set (writes the shared set_status.json). */
function setBags(key, done) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var st = readJson_(STATUS_FILE) || {};
    var e = st[key] || {};
    var clean = {};
    (done || []).forEach(function (b) { b = Number(b); if (b > 0) clean[b] = 1; });
    var arr = Object.keys(clean).map(Number).sort(function (a, b) { return a - b; });
    if (arr.length) { e.status = 'Trier'; e.bags_done = arr; }
    else { delete e.bags_done; }
    if (Object.keys(e).length) st[key] = e; else delete st[key];
    writeJson_(STATUS_FILE, st);
    return arr;
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}

/** Change a set's phase by hand (mirrors the desktop right-click menu):
 *   'ouverture' → clear sorting + all check-offs   (retour avant le tri)
 *   'trier'     → start sorting (keep any check-offs)
 *   'trie'      → mark every bag sorted (check all)  — needs the bag count from the index */
/** « Separation terminee » — le seul geste de la phase « Separer les sacs ».
 *
 *  Repartir les sachets numerotes des N boites en piles par numero est purement PHYSIQUE :
 *  aucun .bsx, aucune base ne peut en temoigner. D'ou ce bouton, et la cle `separated`
 *  dans set_status.json — la seule chose qu'il ecrit.
 *
 *  Il ne choisit AUCUNE phase : la suivante (« Cataloguer ») se deduit des fichiers, comme
 *  tout le reste. C'est ce qui remplace l'ancien « Ouverture des sacs terminee », qui lui
 *  devait trancher entre « Trier » et « En attente de fermeture du batch » — une logique
 *  dupliquee en JavaScript, donc une deuxieme verite a maintenir. */
function finishSeparating(key) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var st = readJson_(STATUS_FILE) || {};
    var e = st[key] || {};
    e.separated = new Date().toISOString().slice(0, 19);
    st[key] = e;
    writeJson_(STATUS_FILE, st);
    return { ok: true, separated: e.separated };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}


function setPhase(key, phase) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var st = readJson_(STATUS_FILE) || {};
    var e = st[key] || {};
    if (phase === 'ouverture') {
      delete e.status; delete e.bags_done;
    } else if (phase === 'trier') {
      e.status = 'Trier';
    } else if (phase === 'trie') {
      var idx = readJson_(INDEX_FILE) || { sets: [] };
      var meta = (idx.sets || []).filter(function (s) { return s.key === key; })[0] || {};
      var bags = Number(meta.bag_count || 0);
      var all = []; for (var b = 1; b <= bags; b++) all.push(b);
      e.status = 'Trier'; e.bags_done = all;
    } else {
      return { ok: false, error: 'phase invalide' };
    }
    if (Object.keys(e).length) st[key] = e; else delete st[key];
    writeJson_(STATUS_FILE, st);
    return { ok: true };
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}

/** Log a purchase by reusing the EXISTING CFB web-app's 'purchase' action (token stays
 *  server-side). Returns the reply text. The daily desktop sync then mirrors it + creates
 *  the CFB folder / inventory. */
/** Inscrit un set au registre d'un batch, avec les MEMES regles que sortpack/batch.add :
 *  batch connu et ouvert, pas deja dans un autre batch ouvert, et idempotent.
 *
 *  On n'ecrit que le registre. Deplacer le dossier du set dans celui du batch est le travail
 *  du PC (`batch.settle`), et de toute facon ce dossier n'existe pas encore : au moment de
 *  l'achat, le set n'est meme pas commande recu. C'est exactement le cas prevu — un set
 *  inscrit avant d'etre catalogue reste au registre en attendant.
 *
 *  Le verrou est celui de queueCommand : deux ecritures du telephone ne peuvent pas se
 *  croiser. Le PC, lui, ecrit le meme fichier via Drive — meme contrat a deux sens que
 *  set_status.json. */
function addToBatch_(setNo, name) {
  name = String(name || '').trim();
  var s = String(setNo || '').trim();
  if (!name || !s) return { ok: false, error: 'batch ou set manquant' };
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var d = readJson_(BATCH_FILE) || {};
    var b = d[name];
    if (!b) return { ok: false, error: 'batch « ' + name + ' » inconnu' };
    if (b.closed) return { ok: false, error: 'le batch « ' + name + ' » est ferme' };
    for (var other in d) {
      if (other !== name && !d[other].closed &&
          (d[other].sets || []).indexOf(s) >= 0) {
        return { ok: false, error: 'le set ' + s + ' est deja dans le batch « ' + other + ' »' };
      }
    }
    b.sets = b.sets || [];
    var already = b.sets.indexOf(s) >= 0;
    if (!already) {
      b.sets.push(s);
      writeJson_(BATCH_FILE, d);
    }
    return { ok: true, name: name, added: !already, sets: b.sets.length };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}


/** Ferme (ou rouvre) un envoi : `closed` dans le registre, rien d'autre.
 *
 *  Fermer veut dire « plus rien n'entre » — addToBatch_ refusera ensuite ce batch. C'est
 *  une DECISION, jamais une consequence du compte de pieces : on peut vouloir glisser un
 *  dernier set en rabais dans un envoi deja au-dela de la cible. Le PC ne fait pas autre
 *  chose (`sortpack.batch.close`), et c'est reversible.
 *
 *  Ne touche ni aux dossiers ni aux fichiers du batch : « terminer » l'envoi (sortir les
 *  copies du stock, envoyer les dossiers a la corbeille) reste un geste de bureau. */
function closeBatch(name, closed) {
  name = String(name || '').trim();
  if (!name) return { ok: false, error: 'batch manquant' };
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
    var d = readJson_(BATCH_FILE) || {};
    var b = d[name];
    if (!b) return { ok: false, error: 'batch « ' + name + ' » inconnu' };
    var want = (closed === false) ? false : true;
    if (!!b.closed === want) {
      return { ok: true, name: name, closed: want, changed: false };
    }
    b.closed = want;
    writeJson_(BATCH_FILE, d);
    return { ok: true, name: name, closed: want, changed: true,
             sets: (b.sets || []).length };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  } finally {
    try { lock.releaseLock(); } catch (x) {}
  }
}


function logPurchase(p) {
  var payload = {
    token: SECRET, action: 'purchase',
    vendor: String((p && p.vendor) || 'Amazon').trim(),
    setNo: String((p && p.number) || '').trim(),
    qty: Math.round(Number((p && p.qty) || 0)),
    amount: Number((p && p.price) || 0),
    compte: 'CC'
  };
  // H Lots / I Pieces du Journal. writePurchase ne les ecrit que s'ils arrivent, et le
  // telephone ne les envoyait pas : les achats saisis ici sortaient sans compte de pieces,
  // contrairement a ceux du bureau. La page les calcule depuis le tableau de bord
  // (comptes PAR COPIE x la quantite achetee), convention Journal = sans les pieces en trop.
  var lots = Math.round(Number((p && p.lots) || 0));
  var pieces = Math.round(Number((p && p.pieces) || 0));
  if (lots > 0) payload.lots = lots;
  if (pieces > 0) payload.pieces = pieces;
  if (p && p.date) payload.date = p.date;
  if (!payload.setNo || payload.qty <= 0 || !payload.amount) {
    return 'error: numéro / quantité / prix manquant';
  }
  var res = UrlFetchApp.fetch(WEBAPP_URL, {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify(payload), followRedirects: true, muteHttpExceptions: true
  });
  var reply = res.getContentText();
  if (String(reply).indexOf('"ok":false') >= 0 || String(reply).indexOf('error') >= 0) {
    return JSON.stringify({ ok: false, error: reply });     // rien inscrit au batch
  }
  // Le batch n'est touche QU'APRES une ligne de Journal reussie : inscrire un achat qui
  // n'existe pas ferait planifier un envoi sur du vide.
  var batch = null;
  if (p && p.batch) batch = addToBatch_(payload.setNo, p.batch);

  // Et on demande la SYNCHRO au PC dans la foulee. Un achat n'est pas fini quand la ligne
  // est ecrite : tant que `purchases.sync` n'a pas tourne, l'achat ne descend pas dans la
  // base locale, le dossier CFB n'existe pas, et « reste a investir » ne bouge pas. Le faire
  // demander a la main, c'est l'oublier.
  //
  // queueCommand refuse un job global deja en attente, donc trois achats de suite ne font
  // qu'une seule synchro -- et elle relit tout le Journal, donc elle les couvre tous.
  var sync = null;
  try {
    sync = queueCommand('', 'sync_purchases');
  } catch (e) {
    sync = { ok: false, error: String((e && e.message) || e) };
  }
  return JSON.stringify({ ok: true, journal: reply, batch: batch, sync: sync });
}
