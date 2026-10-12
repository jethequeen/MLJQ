/**
 * MLJQ — suivi des LIVRAISONS par courriel.
 *
 * Un set acheté passe en « En attente de livraison » et doit en sortir tout seul quand le
 * colis arrive. Deux courriels servent à ça, et aucun des deux ne suffit seul :
 *
 *   Amazon  « Expédié : 6 « LEGO Icons Jaguar E-Type 11381 » »
 *           → dit QUOI et COMBIEN, mais jamais que c'est arrivé.
 *   Intelcom « Hourra! Votre colis est arrivé! »
 *           → dit QUE c'est arrivé, jamais QUOI (le numéro de suivi INTLCM… n'apparaît dans
 *             aucun courriel d'Amazon).
 *
 * On croise donc les deux, et l'appariement est une INFÉRENCE assumée : une livraison
 * constatée solde l'expédition en attente la plus ancienne. C'est faux le jour où deux colis
 * arrivent dans le désordre — d'où le fait que tout soit écrit dans deliveries.json plutôt
 * que décidé ici : le PC garde la trace de ce qu'il a apparié, et on peut le corriger.
 *
 * Portée Gmail : ce fichier LIT la boîte de réception. La première exécution redemandera
 * l'autorisation du projet Apps Script (scope gmail.readonly) — c'est normal.
 *
 * ── INSTALLATION ─────────────────────────────────────────────────────────────
 * 1) Ajouter ce fichier au projet Apps Script du téléphone (il réutilise ses helpers
 *    readJson_ / writeJson_ / cfbFolder_).
 * 2) Déclencheur ▸ Ajouter ▸ scanDeliveries ▸ horaire ▸ toutes les heures.
 * 3) Le PC lit deliveries.json (sortpack/deliveries.py) et fait avancer les phases.
 * ─────────────────────────────────────────────────────────────────────────────
 */

var DELIVERIES_FILE = 'deliveries.json';

// On ne remonte pas indéfiniment : une boîte de réception a des années de commandes, et un
// colis livré il y a six mois n'apprend plus rien.
var DELIVERY_LOOKBACK_DAYS = 60;

// Les expéditeurs. Séparés parce qu'ils disent des choses différentes, pas par symétrie.
var SHIP_QUERY = 'from:shipment-tracking@amazon.ca subject:"Expédié"';
var DELIVERED_QUERY = 'from:notifications@qc.intelcom.ca';
// Costco ne previent pas qu'il expedie : sa CONFIRMATION DE COMMANDE est le premier (et
// souvent le seul) courriel qui dise quoi, combien, et quand c'est attendu. On la traite
// donc comme une expedition — « quelque chose s'en vient » — avec sa date estimee.
var COSTCO_QUERY = 'from:orders.costco.ca';


/** 'il y a N jours' au format que Gmail comprend (yyyy/mm/dd). */
function since_(days) {
  var d = new Date(Date.now() - days * 24 * 3600 * 1000);
  return Utilities.formatDate(d, Session.getScriptTimeZone(), 'yyyy/MM/dd');
}


/** Le numéro de set d'un sujet Amazon.
 *
 *  « Expédié : 6 « LEGO Icons Jaguar E-Type 11381 » » → {set: '11381', qty: 6}
 *
 *  Le numéro est le DERNIER groupe de 4 à 7 chiffres du titre : les noms de sets contiennent
 *  volontiers des nombres (« 1989 Batmobile », « 4x4 »), et LEGO met toujours la référence à
 *  la fin. La quantité est le nombre qui précède le titre, absent quand il n'y en a qu'un. */
function parseShipment_(subject) {
  if (!subject) return null;
  var qty = 1;
  var m = subject.match(/:\s*(\d+)\s*[«"]/);
  if (m) qty = parseInt(m[1], 10) || 1;
  var nums = String(subject).match(/\b\d{4,7}\b/g);
  if (!nums || !nums.length) return null;
  var set = nums[nums.length - 1];
  if (m && set === m[1] && nums.length > 1) set = nums[nums.length - 1];
  return { set: set, qty: qty };
}


var MOIS_ = {january:1, february:2, march:3, april:4, may:5, june:6, july:7, august:8,
             september:9, october:10, november:11, december:12,
             janvier:1, 'février':2, fevrier:2, mars:3, avril:4, mai:5, juin:6, juillet:7,
             'août':8, aout:8, septembre:9, octobre:10, novembre:11, 'décembre':12,
             decembre:12};

/** « Estimated Delivery: Wednesday, October 14 » → '2026-10-14'.
 *
 *  Costco n'ecrit pas l'annee. On prend celle du courriel, et on avance d'un an si la date
 *  tombe plus de deux mois AVANT lui — une commande de decembre livree « January 5 ». */
function parseEta_(body, emailDate, tz) {
  if (!body) return '';
  var m = body.match(/(?:Estimated Delivery|Livraison estim[ée]e)\s*:?\s*(?:[A-Za-zÀ-ÿ]+,?\s*)?([A-Za-zÀ-ÿ]+)\s+(\d{1,2})/i);
  if (!m) return '';
  var mois = MOIS_[m[1].toLowerCase()];
  if (!mois) return '';
  var jour = parseInt(m[2], 10);
  var an = Number(Utilities.formatDate(emailDate, tz, 'yyyy'));
  var d = new Date(an, mois - 1, jour);
  if ((emailDate - d) > 60 * 24 * 3600 * 1000) d = new Date(an + 1, mois - 1, jour);
  return Utilities.formatDate(d, tz, 'yyyy-MM-dd');
}

/** Le set et la quantite d'une confirmation Costco, lus dans le CORPS.
 *
 *  La ligne produit finit par la reference LEGO (« ... Race Car 42226 »), comme chez Amazon.
 *  On ne lit que les lignes qui parlent de LEGO : le corps porte aussi le numero de commande
 *  (10 chiffres), le numero d'article Costco et le numero de membre, tous plus longs. */
function parseCostco_(body) {
  if (!body) return null;
  var set = '', qty = 1;
  var lignes = String(body).split(/[\r\n]+/);
  for (var i = 0; i < lignes.length; i++) {
    if (!/LEGO/i.test(lignes[i])) continue;
    var nums = lignes[i].match(/\b\d{4,7}\b/g);
    if (nums && nums.length) { set = nums[nums.length - 1]; break; }
  }
  if (!set) return null;
  var q = body.match(/(?:Quantity|Quantit[ée])\s*:?\s*(\d+)/i);
  if (q) qty = parseInt(q[1], 10) || 1;
  return { set: set, qty: qty };
}

/** Scrute la boîte et range ce qu'elle dit dans deliveries.json. Idempotent : on réécrit la
 *  photo complète de la fenêtre, le PC se charge de l'apparier. */
function scanDeliveries() {
  var after = ' after:' + since_(DELIVERY_LOOKBACK_DAYS);
  var tz = Session.getScriptTimeZone();

  var shipments = [];
  GmailApp.search(SHIP_QUERY + after, 0, 100).forEach(function (thread) {
    thread.getMessages().forEach(function (msg) {
      var subject = msg.getSubject() || '';
      if (subject.indexOf('Expédié') < 0) return;
      var p = parseShipment_(subject);
      if (!p) return;
      shipments.push({
        set: p.set, qty: p.qty,
        date: Utilities.formatDate(msg.getDate(), tz, 'yyyy-MM-dd'),
        at: Utilities.formatDate(msg.getDate(), tz, "yyyy-MM-dd'T'HH:mm:ss"),
        id: msg.getId(), subject: subject.slice(0, 160), source: 'amazon',
        lane: 'amazon'
      });
    });
  });

  // Costco : la confirmation de commande vaut expedition, avec sa date estimee.
  GmailApp.search(COSTCO_QUERY + after, 0, 100).forEach(function (thread) {
    thread.getMessages().forEach(function (msg) {
      var subject = msg.getSubject() || '';
      if (!/confirm/i.test(subject)) return;
      var body = '';
      try { body = msg.getPlainBody() || ''; } catch (e) { body = ''; }
      var p = parseCostco_(body);
      if (!p) return;
      shipments.push({
        set: p.set, qty: p.qty,
        date: Utilities.formatDate(msg.getDate(), tz, 'yyyy-MM-dd'),
        at: Utilities.formatDate(msg.getDate(), tz, "yyyy-MM-dd'T'HH:mm:ss"),
        id: msg.getId(), subject: subject.slice(0, 160), source: 'costco',
        lane: 'costco', eta: parseEta_(body, msg.getDate(), tz)
      });
    });
  });

  var deliveries = [];
  GmailApp.search(DELIVERED_QUERY + after, 0, 100).forEach(function (thread) {
    thread.getMessages().forEach(function (msg) {
      var subject = msg.getSubject() || '';
      // « Hourra! Votre colis est arrivé! » / « Your package has been delivered »
      if (!/arriv|delivered|livr/i.test(subject)) return;
      var body = '';
      try { body = msg.getPlainBody() || ''; } catch (e) { body = ''; }
      var t = body.match(/INTLCM[A-Z0-9]+/);
      deliveries.push({
        date: Utilities.formatDate(msg.getDate(), tz, 'yyyy-MM-dd'),
        at: Utilities.formatDate(msg.getDate(), tz, "yyyy-MM-dd'T'HH:mm:ss"),
        tracking: t ? t[0] : '', id: msg.getId(),
        subject: subject.slice(0, 160), source: 'intelcom',
        // Intelcom livre pour Amazon : son courriel ne solde que des colis Amazon. Sans ce
        // cloisonnement, le FIFO aurait solde une commande Costco avec un colis Amazon.
        lane: 'amazon'
      });
    });
  });

  shipments.sort(function (a, b) { return a.at < b.at ? -1 : 1; });
  deliveries.sort(function (a, b) { return a.at < b.at ? -1 : 1; });
  writeJson_(DELIVERIES_FILE, {
    generated: Utilities.formatDate(new Date(), tz, "yyyy-MM-dd'T'HH:mm:ss"),
    lookback_days: DELIVERY_LOOKBACK_DAYS,
    shipments: shipments,
    deliveries: deliveries
  });
  return { ok: true, shipments: shipments.length, deliveries: deliveries.length };
}
