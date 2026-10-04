# -*- coding: utf-8 -*-
"""
Read the per-set "Quantité" from the Inventaire Google Sheet, via the Apps Script
Web App's doGet (see appscript/CFB_email.gs). Kept dependency-free (stdlib only) and
independent of plan/finalize so both can import it without a cycle.
"""

import json
import urllib.request
import urllib.parse
import urllib.error

from . import config


def _get_json(action, timeout=30):
    """GET <apps-script>?action=<action> and parse the JSON reply. Uses the same Web App
    as the buy-report POST. Raises on a non-JSON (login/HTML) reply."""
    url = config.BUY_REPORT_POST_URL or config.CFB_APPS_SCRIPT_URL
    if not url:
        raise RuntimeError("aucune URL Apps Script configurée")
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"action": action})
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    head = text[:200].lstrip().lower()
    if head.startswith("<") or "<html" in head:
        raise RuntimeError("réponse HTML (Web App non déployé avec doGet / accès)")
    return json.loads(text)


def fetch_params():
    """The Paramètres tab as {key: value}. Empty dict if unavailable."""
    return _get_json("params").get("params", {})


def fetch_transactions():
    """The Journal's Catégorie-'A' rows: [{'transaction':…, 'date':…}, …]."""
    return _get_json("transactions").get("transactions", [])


def fetch_finance():
    """Financial results sheet → {'rate': MLJQ monthly-invest share (e.g. 0.30),
    'deltas': {'yyyy-mm': fortune_delta, …}}. The monthly budget for a month M is
    deltas[M-1] × rate (last month's net worth growth × MLJQ's share)."""
    d = _get_json("finance")
    return {"rate": d.get("rate"), "deltas": d.get("deltas") or {}}


def post_buy_list(rows, header=None, url=None, tab=None, email=True, warning=None):
    """POST the ranked buy list (read from the history DB — see
    HistoryDB.latest_buy_list) to the Apps Script Web App (action 'buylist'), which
    writes it to the sheet's Achats tab. Requires config.BUY_REPORT_POST_URL. `warning`,
    if set, is shown as a banner at the top of the email. Returns the script's text
    response ('ok: N rows')."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré dans sortpack/config.py")
    if header is None:
        from .history import BUY_SHEET_COLS
        header = BUY_SHEET_COLS
    payload = {
        "token": config.CFB_APPS_SCRIPT_TOKEN,
        "action": "buylist",
        "tab": tab or config.BUY_REPORT_SHEET_TAB,
        "header": list(header),
        "rows": [list(r) for r in rows],
    }
    if warning:
        payload["warning"] = warning
    if email:                            # send the HTML email only when asked
        to = getattr(config, "BUY_REPORT_EMAIL_TO", None) or config.CFB_EMAIL_TO
        if not isinstance(to, str):
            to = ", ".join(to)
        payload["email"] = to
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace").strip()


def post_sales_week(date, source, total, fees, lots, parts, url=None):
    """POST one source's weekly CFB sales to the Apps Script 'salesweek' action, which
    appends the Ventes (V) + Frais CFB (FT) rows to the Journal tab. `date` is the week-
    ending Friday (yyyy-mm-dd); `total`/`fees` are the amounts to book (CA tax-included).
    Returns the script's JSON text reply."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré")
    payload = {
        "token": config.CFB_APPS_SCRIPT_TOKEN,
        "action": "salesweek",
        "date": date, "source": source,
        "total": total, "fees": fees, "lots": lots, "parts": parts,
    }
    data = json.dumps(payload).encode("utf-8")
    # Apps Script POSTs answer with a 302 to googleusercontent that occasionally 404s on the
    # follow; retry a couple of times. Safe because writeSalesWeek is idempotent (a re-post
    # for the same source+date is skipped, never duplicated).
    import time
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 500, 502, 503, 429) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def post_inventory_value(month, value, pieces, url=None, fin_col=23, sommaire=True,
                         vendor=None):
    """POST a seller's CFB inventory value/pieces to the Apps Script 'invvalue' action, which
    writes them to the given month's row of the financial 'Résultats mensuels' sheet, column
    `fin_col` (1-based; 22=V Inventaire Binobrick, 23=W Inventaire MLJQ). When `sommaire` is
    true it also updates the accounting 'Sommaire mensuel' B (Inventaire estimé) + D (Pièces)
    — that summary is MLJQ's own inventory, so Binobrick passes sommaire=False. `month` =
    yyyy-mm; `vendor` is passed through for the script's log only. Returns the JSON reply."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré")
    payload = {"token": config.CFB_APPS_SCRIPT_TOKEN, "action": "invvalue",
               "month": month, "value": value, "pieces": pieces,
               "finCol": fin_col, "sommaire": bool(sommaire), "vendor": vendor}
    data = json.dumps(payload).encode("utf-8")
    import time
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def post_monthly_budget(month, amount, url=None):
    """POST the month's investable budget (prior month's net-worth Delta × MLJQ rate) to the
    Apps Script 'budget' action, which writes it to that month's row of the accounting
    'Sommaire mensuel' tab, column G ("Montant à investir"). Informational only. `month` =
    yyyy-mm. Returns the JSON text reply."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré")
    payload = {"token": config.CFB_APPS_SCRIPT_TOKEN, "action": "budget",
               "month": month, "amount": amount}
    data = json.dumps(payload).encode("utf-8")
    import time
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def post_purchase(vendor, set_no, qty, amount, date=None, compte="CC",
                  lots=0, pieces=0, url=None):
    """POST a LEGO purchase to the Apps Script 'purchase' action, which appends a Catégorie-'A'
    row to the Journal: A="<vendor> - <set_no> (<qty>)", B="A", C=−amount (tax-included,
    stored negative), D=compte ("CC"), E=date, plus H=lots / I=pieces (the total bought =
    per-set counts × qty). TPS/TVQ are the sheet's own formulas. `amount` is the tax-included
    TOTAL price paid (pass positive; it's stored negative). `set_no` is the bare set number;
    `date`=yyyy-mm-dd (default = sheet's today). Returns the JSON text reply."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré")
    payload = {"token": config.CFB_APPS_SCRIPT_TOKEN, "action": "purchase",
               "vendor": vendor, "setNo": str(set_no), "qty": int(qty),
               "amount": float(amount), "compte": compte,
               "lots": int(lots or 0), "pieces": int(pieces or 0)}
    if date:
        payload["date"] = date
    data = json.dumps(payload).encode("utf-8")
    import time
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def post_reste_a_investir(amount, tab=None, cell=None, url=None):
    """POST the current 'reste à investir' (cumulative available budget) to the Apps Script
    'dashval' action, which writes it to one cell of the accounting dashboard tab
    (config.DASHBOARD_TAB!DASHBOARD_CELL by default), overwritten each run. Returns the JSON
    text reply."""
    url = url or config.BUY_REPORT_POST_URL
    if not url:
        raise RuntimeError("BUY_REPORT_POST_URL non configuré")
    payload = {"token": config.CFB_APPS_SCRIPT_TOKEN, "action": "dashval",
               "tab": tab or config.DASHBOARD_TAB, "cell": cell or config.DASHBOARD_CELL,
               "amount": float(amount)}
    data = json.dumps(payload).encode("utf-8")
    import time
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last


def send_alert(subject, message, to=None, url=None):
    """Send a plain notification email via the Apps Script (default doPost branch — no
    files, no tab write). Used for the CFB cookie-expiry alert. Returns the text reply."""
    url = url or config.BUY_REPORT_POST_URL or config.CFB_APPS_SCRIPT_URL
    if not url:
        raise RuntimeError("aucune URL Apps Script configurée")
    payload = {
        "token": config.CFB_APPS_SCRIPT_TOKEN,
        "to": to or config.CFB_EMAIL_TO,
        "subject": subject,
        "message": message,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace").strip()


class SetNotInSheet(RuntimeError):
    """The set number has no row in the Inventaire sheet — no Quantité to multiply by.
    Distinct from generic failures (network, bad deployment) so the caller can tell the
    user to add the row rather than blaming the connection."""


def fetch_set_quantity(set_no):
    """
    GET <apps-script-url>?set=<set_no> and return the Quantité as an int.
    Raises RuntimeError if the URL isn't configured or the set isn't found.
    """
    url = config.CFB_APPS_SCRIPT_URL
    if not url:
        raise RuntimeError("CFB_APPS_SCRIPT_URL non configuré dans sortpack/config.py")

    q = urllib.parse.urlencode({"set": str(set_no)})
    full = url + ("&" if "?" in url else "?") + q
    with urllib.request.urlopen(full, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace").strip()

    head = text[:200].lstrip().lower()
    if head.startswith("<") or "<!doctype" in head or "<html" in head:
        raise RuntimeError(
            "page HTML reçue (login/consentement Google) — le Web App n'est pas déployé "
            "avec doGet ou l'accès n'est pas « Anyone »")
    if "not found" in text.lower() and set_no.lower() in text.lower():
        raise SetNotInSheet(
            f"le set {set_no} n'a pas de ligne dans la feuille Inventaire")
    if not text or text.startswith("error:") or text.lower() == "unauthorized":
        raise RuntimeError(f"set {set_no} : {text[:120]!r}")
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        raise RuntimeError(f"réponse inattendue : {text[:120]!r}")
