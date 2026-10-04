/**
 * CFB Apps Script — one Web App, two actions:
 *   doPost  → email the consolidated .bsx files back as attachments.
 *   doGet   → look up a set's "Quantité" in the Inventaire sheet, so the Python
 *             side can multiply the bag quantities by it when writing remarks.
 *
 * Deploy: Apps Script editor → Deploy → New deployment → type "Web app" →
 *   Execute as: Me, Who has access: Anyone → copy the /exec URL.
 * In sortpack/config.py set CFB_APPS_SCRIPT_URL to that URL and
 * CFB_APPS_SCRIPT_TOKEN to the same string as SECRET below.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 *  COLUMN / SHEET MAPPING — adjust here if the Inventaire sheet layout changes.
 * ─────────────────────────────────────────────────────────────────────────────
 */

// The spreadsheet holding the Inventaire tab. Paste the ID from its URL:
//   https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit
const SPREADSHEET_ID = '1Zsb_Tak7GuVvpy3S4liyA7W657yyHI25VM-xy23cDCI';

// The FINANCIAL results spreadsheet (separate file) — drives the dynamic monthly budget.
// 'Résultats mensuels' tab: A=Mois, B=Fortune, C=Delta. 'Config' tab: B14 = MLJQ % invest.
const FINANCE_SPREADSHEET_ID = '1a5TexN13Da29Yw2HkyfEbIm_YrUfTTfejWAbmFXH18k';
const FINANCE_RESULTS_TAB = 'Résultats mensuels';
const FINANCE_CONFIG_TAB  = 'Config';
const FINANCE_MLJQ_RATE_CELL = 'B14';           // MLJQ share of post-REER monthly invest

const SHEET_NAME  = 'Inventaire';  // tab name
const HEADER_ROW  = 3;             // headers are on row 3; data starts on row 4
const DATA_START_ROW = 4;

// 1-based column numbers (A=1, B=2, …). From the current layout:
const COL_SETS      = 1;   // A — Sets (nom long)
const COL_NUMERO    = 2;   // B — Numéro de set   ← lookup key
const COL_THEME     = 3;   // C — Thème
const COL_YEAR      = 4;   // D — Year
const COL_NAME      = 5;   // E — Name
const COL_NBR_PIECE = 6;   // F — Nbr pièce
const COL_RRP       = 7;   // G — RRP
const COL_VALEUR    = 8;   // H — Valeur
const COL_NBR_LOTS  = 9;   // I — Nbr lots
const COL_QUANTITE  = 10;  // J — Quantité       ← the multiplier we return
const COL_RESTANTES = 11;  // K — Pièces restantes

// Shared secret: a leaked URL alone must not trigger emails. Change it, and set
// the same value in CFB_APPS_SCRIPT_TOKEN in config.py.
const SECRET = 'zxzxws12';

// ─────────────────────────────────────────────────────────────────────────────

/**
 * doGet — actions:
 *   ?action=transactions → JSON {transactions:[{transaction,date},…]} : the Journal's
 *                          Catégorie-"A" (LEGO purchase) rows.
 *   ?action=params       → JSON {params:{key:value,…}} : the Paramètres tab (col A→B).
 *   ?action=finance      → JSON {rate, deltas:{yyyy-MM:delta}} : the financial sheet, to
 *                          compute the dynamic monthly budget (Delta of prev month × rate).
 *   ?set=76342           → the Quantité (plain text; legacy).
 */
function doGet(e) {
  const action = String((e.parameter && e.parameter.action) || '').trim();
  if (action === 'transactions') return getTransactions_();
  if (action === 'params') return getParams_();
  if (action === 'finance') return getFinance_();
  try {
    const setNo = String((e.parameter && e.parameter.set) || '').trim();
    if (!setNo) return ContentService.createTextOutput('error: missing ?set=');

    const sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SHEET_NAME);
    if (!sheet) return ContentService.createTextOutput('error: sheet not found');

    const last = sheet.getLastRow();
    if (last < DATA_START_ROW) return ContentService.createTextOutput('error: no data');

    const n = last - DATA_START_ROW + 1;
    const numeros = sheet.getRange(DATA_START_ROW, COL_NUMERO, n, 1).getValues();
    const qtys    = sheet.getRange(DATA_START_ROW, COL_QUANTITE, n, 1).getValues();

    for (let i = 0; i < n; i++) {
      if (String(numeros[i][0]).trim() === setNo) {
        return ContentService.createTextOutput(String(qtys[i][0]).trim());
      }
    }
    return ContentService.createTextOutput('error: set ' + setNo + ' not found');
  } catch (err) {
    return ContentService.createTextOutput('error: ' + err);
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** The Journal's Catégorie-"A" rows. Cols: A=Transaction, B=Catégorie, C=Montant (tax
 *  included), E=Date, J=TPS, K=TVQ. */
function getTransactions_() {
  try {
    const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName('Journal');
    if (!sh) return json_({ error: 'Journal tab not found' });
    const last = sh.getLastRow();
    const out = [];
    if (last >= 2) {
      const vals = sh.getRange(2, 1, last - 1, 11).getValues();  // cols A..K
      const tz = Session.getScriptTimeZone();
      vals.forEach(function (r) {
        if (String(r[1]).trim() === 'A') {                        // B = Catégorie
          var d = r[4];                                           // E = Date
          if (d instanceof Date) d = Utilities.formatDate(d, tz, 'yyyy-MM-dd');
          out.push({
            transaction: String(r[0]).trim(),
            date: String(d).trim(),
            montant: r[2],                                        // C = Montant (incl tax)
            tps: r[9],                                            // J = TPS
            tvq: r[10]                                            // K = TVQ
          });
        }
      });
    }
    return json_({ transactions: out });
  } catch (err) {
    return json_({ error: String(err) });
  }
}

// French month names → month number, for parsing a text "Mois" column ("juillet 2026").
const FR_MONTHS = {
  janvier: 1, 'février': 2, fevrier: 2, mars: 3, avril: 4, mai: 5, juin: 6,
  juillet: 7, 'août': 8, aout: 8, septembre: 9, octobre: 10, novembre: 11,
  'décembre': 12, decembre: 12
};

/** A "Mois" cell (a Date, or French text like "juillet 2026") → "yyyy-MM", or '' if unparsable. */
function financeMonthKey_(v, tz) {
  if (v instanceof Date) return Utilities.formatDate(v, tz, 'yyyy-MM');
  const s = String(v).trim().toLowerCase();
  const m = s.match(/([a-zàâäéèêëîïôöûüç]+)\s+(\d{4})/);
  if (m && FR_MONTHS[m[1]]) {
    const mm = FR_MONTHS[m[1]];
    return m[2] + '-' + (mm < 10 ? '0' + mm : '' + mm);
  }
  return '';
}

/** Financial sheet → {rate: MLJQ invest %, deltas:{ "yyyy-MM": fortuneDelta, … }}.
 *  The monthly budget for month M = deltas[M-1] × rate (last month's growth × MLJQ share). */
function getFinance_() {
  try {
    const ss = SpreadsheetApp.openById(FINANCE_SPREADSHEET_ID);
    const rm = ss.getSheetByName(FINANCE_RESULTS_TAB);
    const cfg = ss.getSheetByName(FINANCE_CONFIG_TAB);
    if (!rm || !cfg) return json_({ error: 'finance tabs not found' });
    const tz = ss.getSpreadsheetTimeZone();
    const rate = Number(cfg.getRange(FINANCE_MLJQ_RATE_CELL).getValue());
    const deltas = {};
    const last = rm.getLastRow();
    if (last >= 2) {
      const vals = rm.getRange(2, 1, last - 1, 3).getValues();   // A=Mois, B=Fortune, C=Delta
      vals.forEach(function (r) {
        const key = financeMonthKey_(r[0], tz);
        const d = r[2];
        if (key && typeof d === 'number' && isFinite(d)) deltas[key] = d;
      });
    }
    return json_({ rate: rate, deltas: deltas });
  } catch (err) {
    return json_({ error: String(err) });
  }
}

/** The Paramètres tab as {key: value} from columns A (key) and B (value). */
function getParams_() {
  try {
    const sh = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName('Paramètres');
    if (!sh) return json_({ params: {} });
    const last = sh.getLastRow();
    const params = {};
    if (last >= 1) {
      const vals = sh.getRange(1, 1, last, 2).getValues();
      vals.forEach(function (r) {
        const k = String(r[0]).trim();
        if (k) params[k] = r[1];
      });
    }
    return json_({ params: params });
  } catch (err) {
    return json_({ error: String(err) });
  }
}

/**
 * doPost — two actions, selected by body.action:
 *   'buylist' {token, action, tab, header:[…], rows:[[…],…]} → overwrite the buy-list
 *             tab with the ranked report.
 *   (default) {token, to, subject, message, files:[…]}       → email the CFB files.
 */
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.token !== SECRET) {
      return ContentService.createTextOutput('unauthorized');
    }

    if (body.action === 'buylist') return writeBuyList(body);
    if (body.action === 'salesweek') return writeSalesWeek(body);
    if (body.action === 'invvalue') return writeInventoryValue(body);
    if (body.action === 'budget') return writeMonthlyBudget(body);
    if (body.action === 'purchase') return writePurchase(body);
    if (body.action === 'dashval') return writeDashboardValue(body);

    const attachments = (body.files || []).map(function (f) {
      return Utilities.newBlob(
        Utilities.base64Decode(f.contentB64),
        'application/xml',
        f.filename
      );
    });

    MailApp.sendEmail({
      to: body.to,
      subject: body.subject || 'Fichier CFB',
      body: body.message || 'Fichiers ci-joints.',
      attachments: attachments
    });

    return ContentService.createTextOutput('ok');
  } catch (err) {
    return ContentService.createTextOutput('error: ' + err);
  }
}

/** Overwrite the buy-list tab with header + rows, plus a "generated" timestamp. */
function writeBuyList(body) {
  const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
  const tabName = body.tab || 'Achats';
  let sheet = ss.getSheetByName(tabName);
  if (!sheet) sheet = ss.insertSheet(tabName);
  sheet.clearContents();

  sheet.getRange(1, 1).setValue('Généré : ' + new Date());
  const header = body.header || [];
  const rows = body.rows || [];
  const urlCol = header.indexOf('url');

  if (header.length) {
    sheet.getRange(2, 1, 1, header.length).setValues([header]);
  }
  if (rows.length) {
    // write the grid (blank the url cells — they get a real link below)
    const grid = rows.map(function (r) {
      r = r.slice();
      if (urlCol >= 0) r[urlCol] = '';
      return r;
    });
    sheet.getRange(3, 1, rows.length, header.length).setValues(grid);

    // real rich-text hyperlinks (no formula → no ,-vs-; locale problem)
    if (urlCol >= 0) {
      const links = rows.map(function (r) {
        const u = r[urlCol];
        const rt = u
          ? SpreadsheetApp.newRichTextValue().setText('Acheter').setLinkUrl(String(u)).build()
          : SpreadsheetApp.newRichTextValue().setText('').build();
        return [rt];
      });
      sheet.getRange(3, urlCol + 1, rows.length, 1).setRichTextValues(links);
    }
  }
  if (body.email && (rows.length || body.warning)) {
    sendBuyEmail_(body.email, header, rows, body.warning);
  }
  return ContentService.createTextOutput('ok: ' + rows.length + ' rows');
}

// ── CFB inventory value → month rows ─────────────────────────────────────────
// Writes one seller's inventory selling value + pieces to the row of the given month:
//   financial  "Résultats mensuels" → column `finCol` (22=V Inventaire Binobrick,
//                                      23=W Inventaire MLJQ; default 23)
//   accounting "Sommaire mensuel"    → B (Inventaire estimé) + D (Pièces), ONLY when
//                                      body.sommaire is not false — that summary is MLJQ's
//                                      own inventory, so Binobrick sends sommaire=false.
// month is "yyyy-MM"; the Mois column may be a Date or French text ("août 2026").
function writeInventoryValue(body) {
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(30000);
    var month = String(body.month || '');
    if (!/^\d{4}-\d{2}$/.test(month)) return json_({ ok: false, error: 'bad month ' + month });
    var value = Number(body.value || 0);
    var pieces = Number(body.pieces || 0);
    var finCol = Number(body.finCol) || 23;           // 22=V Binobrick, 23=W MLJQ
    var wantSommaire = body.sommaire !== false;        // default true (legacy behaviour)
    var out = { ok: true, month: month, vendor: body.vendor || null, finCol: finCol };

    if (wantSommaire) {
      var acc = SpreadsheetApp.openById(SPREADSHEET_ID);
      var accSheet = acc.getSheetByName('Sommaire mensuel');
      var accRow = accSheet ? findMonthRow_(accSheet, month, acc.getSpreadsheetTimeZone()) : null;
      if (accRow) {
        accSheet.getRange(accRow, 2).setValue(value);    // B Inventaire estimé
        accSheet.getRange(accRow, 4).setValue(pieces);   // D Pièces
      }
      out.sommaireRow = accRow;
    }

    var fin = SpreadsheetApp.openById(FINANCE_SPREADSHEET_ID);
    var finSheet = fin.getSheetByName(FINANCE_RESULTS_TAB);
    var finRow = finSheet ? findMonthRow_(finSheet, month, fin.getSpreadsheetTimeZone()) : null;
    if (finRow) finSheet.getRange(finRow, finCol).setValue(value);   // V or W
    out.resultatsRow = finRow;

    SpreadsheetApp.flush();
    return json_(out);
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { /* ignore */ }
  }
}

// ── Reste à investir → Dashboard cell ────────────────────────────────────────
// Writes ONE running value (the cumulative available budget) to a single cell of the
// accounting spreadsheet's dashboard tab (tab/cell come from the caller;
// config.DASHBOARD_TAB / DASHBOARD_CELL). Overwritten on every daily run.
function writeDashboardValue(body) {
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(30000);
    var tab = String(body.tab || 'Dashboard');
    var cell = String(body.cell || 'G3');
    var amount = Number(body.amount || 0);
    var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = ss.getSheetByName(tab);
    if (!sheet) return json_({ ok: false, error: 'onglet "' + tab + '" introuvable' });
    sheet.getRange(cell).setValue(amount);
    SpreadsheetApp.flush();
    return json_({ ok: true, tab: tab, cell: cell, amount: amount });
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { /* ignore */ }
  }
}

// ── Monthly amount-to-invest → Sommaire mensuel ──────────────────────────────
// Writes the computed investable budget (prior month's net-worth Delta × MLJQ rate)
// to the given month's row of the accounting 'Sommaire mensuel' tab, column G
// (7 = "Montant à investir"). Informational only — nothing else reads it back.
// month is "yyyy-MM"; the Mois column may be a Date or French text ("septembre 2026").
function writeMonthlyBudget(body) {
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(30000);
    var month = String(body.month || '');
    if (!/^\d{4}-\d{2}$/.test(month)) return json_({ ok: false, error: 'bad month ' + month });
    var amount = Number(body.amount || 0);
    var acc = SpreadsheetApp.openById(SPREADSHEET_ID);
    var sheet = acc.getSheetByName('Sommaire mensuel');
    var row = sheet ? findMonthRow_(sheet, month, acc.getSpreadsheetTimeZone()) : null;
    if (row) sheet.getRange(row, 7).setValue(amount);   // G — Montant à investir
    SpreadsheetApp.flush();
    return json_({ ok: true, month: month, amount: amount, budgetRow: row, col: 7 });
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { /* ignore */ }
  }
}

// ── LEGO purchase → Journal (Catégorie A) ────────────────────────────────────
// Appends ONE purchase row to the Journal, in the exact format purchases.py expects:
//   A = "<vendeur> - <numéro> (<qté>)"   e.g. "Amazon - 76342 (10)"
//   B = "A"        (Catégorie: LEGO purchase)
//   C = −amount    (Montant, tax-INCLUDED, stored negative like a hand entry)
//   D = "CC"       (Compte)
//   E = date       (purchase date)
// J/K (TPS/TVQ) are the sheet's own formulas — never written here. The daily
// purchases.sync then mirrors this row into the local bought/inventory + CFB folder.
function writePurchase(body) {
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(30000);
    var sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SALES_JOURNAL_TAB);
    if (!sheet) return json_({ ok: false, error: 'onglet "' + SALES_JOURNAL_TAB + '" introuvable' });
    var setNo = String(body.setNo || '').trim();
    var vendor = String(body.vendor || 'Amazon').trim();
    var qty = Math.round(Number(body.qty || 0));
    var amount = salesRound2_(Number(body.amount || 0));
    var lots = Math.round(Number(body.lots || 0));
    var pieces = Math.round(Number(body.pieces || 0));
    if (!setNo || qty <= 0 || !amount) return json_({ ok: false, error: 'setNo/qty/amount manquant' });
    if (amount > 0) amount = -amount;                       // a purchase is an expense (negative)
    var compte = String(body.compte || 'CC');
    var date = body.date ? salesParseYmd_(body.date) : new Date();
    var label = vendor + ' - ' + setNo + ' (' + qty + ')';
    var row = salesNextRows_(sheet, 1)[0];
    sheet.getRange(row, 1, 1, 5).setValues([[ label, 'A', amount, compte, date ]]);
    if (lots || pieces) sheet.getRange(row, 8, 1, 2).setValues([[ lots, pieces ]]);   // H Lots, I Pièces
    SpreadsheetApp.flush();
    return json_({ ok: true, row: row, transaction: label, amount: amount,
                   compte: compte, lots: lots, pieces: pieces });
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { /* ignore */ }
  }
}

/** Row whose Mois cell (col A, Date or French text) normalizes to `month` (yyyy-MM), or null. */
function findMonthRow_(sheet, month, tz) {
  var last = sheet.getLastRow();
  if (last < 2) return null;
  var vals = sheet.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < vals.length; i++) {
    if (financeMonthKey_(vals[i][0], tz) === month) return i + 2;
  }
  return null;
}

// ── CFB weekly sales → Journal ────────────────────────────────────────────────
// Appends two rows per call to the 'Journal' tab (after the last A-populated row):
//   1) "Ventes - CA/US"    catégorie V,  Montant = gross (CA tax-included), + Lots/Pièces
//   2) "Frais CFB - CA/US" catégorie FT, Montant = -fees  (only if fees > 0)
// J/K (TPS/TVQ) and L (CMV) are left to the sheet's own formulas. Same layout as boxoffice.
var SALES_JOURNAL_TAB = 'Journal';
var SALES_COMPTE = 'CFB';

function writeSalesWeek(body) {
  var lock = LockService.getDocumentLock();
  try {
    lock.waitLock(30000);
    if (!body.date) return json_({ ok: false, error: 'missing date' });
    var sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SALES_JOURNAL_TAB);
    if (!sheet) return json_({ ok: false, error: 'onglet "' + SALES_JOURNAL_TAB + '" introuvable' });

    var date = salesParseYmd_(body.date);
    var source = String(body.source || 'CA').toUpperCase();
    var total = salesRound2_(Number(body.total || 0));
    var fees = salesRound2_(Number(body.fees || 0));
    var lots = Number(body.lots || 0);
    var parts = Number(body.parts || 0);

    // Idempotency: if a "Ventes - <source>" row for this date already exists, don't append
    // again (a flaky 302/404 on the client side must never double-book). Returns ventesRow
    // so the caller still marks the week done.
    var already = salesFindRow_(sheet, 'Ventes - ' + source, body.date);
    if (already) {
      return json_({ ok: true, skipped: 'already posted', ventesRow: already,
                     source: source, date: body.date });
    }

    var rows = salesNextRows_(sheet, 2);
    // Ventes row: A:E + H:I
    sheet.getRange(rows[0], 1, 1, 5).setValues([[
      'Ventes - ' + source, 'V', total, SALES_COMPTE, date]]);
    sheet.getRange(rows[0], 8, 1, 2).setValues([[lots, parts]]);
    var fraisRow = null;
    if (fees > 0) {
      fraisRow = rows[1];
      sheet.getRange(fraisRow, 1, 1, 5).setValues([[
        'Frais CFB - ' + source, 'FT', -fees, SALES_COMPTE, date]]);
    }
    SpreadsheetApp.flush();
    return json_({ ok: true, ventesRow: rows[0], fraisRow: fraisRow, source: source, date: body.date });
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  } finally {
    try { lock.releaseLock(); } catch (e2) { /* ignore */ }
  }
}

/** Row number of an existing Transaction (col A) with a matching Date (col E, yyyy-MM-dd),
 *  or null. Used to make writeSalesWeek idempotent against client retries. */
function salesFindRow_(sheet, name, dateStr) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return null;
  var vals = sheet.getRange(2, 1, lastRow - 1, 5).getValues();   // A:E
  var tz = Session.getScriptTimeZone();
  for (var i = 0; i < vals.length; i++) {
    if (String(vals[i][0]).trim() !== name) continue;
    var d = vals[i][4];
    var ds = (d instanceof Date) ? Utilities.formatDate(d, tz, 'yyyy-MM-dd')
                                 : String(d).slice(0, 10);
    if (ds === String(dateStr).slice(0, 10)) return i + 2;
  }
  return null;
}

/** `n` consecutive rows right after the last A-populated row (scan col A bottom-up, so we
 *  append after the latest transaction rather than filling a historical gap). */
function salesNextRows_(sheet, n) {
  var out = [];
  var lastRow = sheet.getLastRow();
  var start = 2;
  if (lastRow >= 2) {
    var aVals = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = aVals.length - 1; i >= 0; i--) {
      if (aVals[i][0] !== '' && aVals[i][0] !== null) { start = i + 3; break; }
    }
  }
  for (var k = 0; k < n; k++) out.push(start + k);
  return out;
}

function salesParseYmd_(s) {
  var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : new Date(s);
}

function salesRound2_(n) { return Math.round(Number(n || 0) * 100) / 100; }

/** A "44%" / "0%" / "" discount string → the number 44 (0 if none/unparsable). */
function discountPct_(v) {
  const m = String(v == null ? '' : v).match(/(\d+(?:[.,]\d+)?)/);
  return m ? Number(m[1].replace(',', '.')) : 0;
}

/** Clean HTML email of the batch: optional warning banner · picture · name + set #
 *  · pieces · struck original price + current price (when discounted) · Acheter. */
function sendBuyEmail_(to, header, rows, warning) {
  const i = {};
  header.forEach(function (h, k) { i[h] = k; });
  let html = '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:520px">';
  if (warning) {
    html += '<div style="background:#fff3cd;border:1px solid #ffe08a;color:#8a6d00;'
      + 'border-radius:6px;padding:11px 14px;margin-bottom:14px;font-size:13px">⚠ '
      + warning + '</div>';
  }
  html += '<h2 style="font-weight:600;color:#1f2328">Lot du mois</h2>'
    + '<table style="border-collapse:collapse;width:100%">';
  rows.forEach(function (r) {
    const sid = r[i.set_id];
    const bare = String(sid).split('-')[0];          // "11503-1" → "11503"
    const img = 'https://images.brickset.com/sets/large/' + sid + '.jpg';
    const url = r[i.url];
    const price = Number(r[i.price_cad] || 0);
    const pct = discountPct_(r[i.discount]);
    // Price line: current price, with the pre-discount price struck through when on sale.
    let priceHtml = '<span style="font-weight:600;color:#1f2328">$' + price.toFixed(2) + '</span>';
    if (pct > 0 && pct < 100) {
      const orig = price / (1 - pct / 100);
      priceHtml = '<span style="color:#999;text-decoration:line-through">$' + orig.toFixed(2)
        + '</span> ' + priceHtml
        + ' <span style="color:#1a7f37;font-weight:600">−' + Math.round(pct) + '%</span>';
    }
    // Pieces (from the catalog, appended by job.run); may be '' if unavailable.
    const pieces = (i.pieces != null) ? r[i.pieces] : '';
    const piecesHtml = (pieces !== '' && pieces != null)
      ? ' · ' + Number(pieces).toLocaleString() + ' pièces' : '';
    html += '<tr style="border-bottom:1px solid #eee">'
      + '<td style="padding:12px" width="112"><img src="' + img + '" width="100" '
      + 'style="border-radius:6px"></td>'
      + '<td style="padding:12px;vertical-align:middle">'
      + '<div style="font-size:15px;font-weight:600;color:#1f2328">' + r[i.name] + '</div>'
      + '<div style="color:#888;margin:2px 0 6px;font-size:12px">#' + bare + piecesHtml + '</div>'
      + '<div style="margin:0 0 9px;font-size:13px">' + priceHtml + '</div>'
      + (url ? '<a href="' + url + '" style="display:inline-block;background:#0969da;color:#fff;'
        + 'padding:7px 18px;text-decoration:none;border-radius:5px;font-size:13px">Acheter</a>' : '')
      + '</td></tr>';
  });
  html += '</table></div>';
  MailApp.sendEmail({ to: to, subject: 'Lot du mois — MLJQ', htmlBody: html });
}
