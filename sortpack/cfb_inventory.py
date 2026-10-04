# -*- coding: utf-8 -*-
"""
Read the TOTAL INVENTORY SELLING VALUE from the Canada First Bricks vendor portals'
batch inventory reports — fully in Python, reusing the cookie/CSRF/auth pattern proven in
the boxoffice-quebec project (netlify/lib/cfb.js), but standalone here.

Two portals, same Rails app, different host + cookie:
    CA  https://mocs.canadafirstbricks.com    cookie env  CFB_COOKIE
    US  https://usmocs.canadafirstbricks.com  cookie env  CFB_COOKIE_US

Flow per source (synchronous, like the sales report):
    GET  /bricklink/inventory_vendor_batch_reports/new   -> CSRF + form fields
    POST the form (action)                               -> 302 -> rendered report HTML
    parse the total selling value + total pieces from the top of the report

An expired cookie bounces to the login page; we raise CfbAuthError instead of recording a
false zero (the boxoffice project lost a day exactly that way).

Cookies are NOT stored here — set the two env vars (the same values boxoffice uses). Keep
them fresh yourself; a stale cookie raises a clear auth error.

STATUS: the fetch/auth machinery is complete; the report PARSER (`parse_batch_totals`) is a
first-pass heuristic pending a real report sample — run `python -m sortpack.cfb_inventory
--dump` to save the raw HTML, then the parser is finalised against it.
"""

import os
import re
import sys
import html
import datetime
import http.cookiejar
import urllib.request
import urllib.parse
from html.parser import HTMLParser

NEW_PATH = "/bricklink/inventory_vendor_batch_reports/new"
POST_PATH = "/bricklink/inventory_vendor_batch_reports"

SOURCES = {
    "CA": {"base": "https://mocs.canadafirstbricks.com",   "cookie_env": "CFB_COOKIE"},
    "US": {"base": "https://usmocs.canadafirstbricks.com", "cookie_env": "CFB_COOKIE_US"},
}


class CfbAuthError(RuntimeError):
    """The session cookie has expired (request bounced to login). Distinct so callers flag
    the run instead of recording a false zero inventory value."""
    def __init__(self, source, detail=""):
        super().__init__(f"CFB[{source}] session expirée{(' — ' + detail) if detail else ''}")
        self.source = source


class VendorNotFound(RuntimeError):
    """The requested seller isn't in the portal's vendor dropdown (e.g. MLJQ before that
    seller is created). Distinct so a multi-vendor capture can SKIP it and keep going,
    rather than aborting the whole run."""
    def __init__(self, source, vendor, available):
        super().__init__(
            f"CFB[{source}] : vendeur « {vendor} » introuvable dans le portail — "
            f"vendeurs disponibles : {available or '(aucun)'}")
        self.source = source
        self.vendor = vendor


def get_cookie(source):
    """Effective cookie for a source: the value stored via the GUI (DB meta) if present,
    else the env-var fallback (CFB_COOKIE / CFB_COOKIE_US). None if neither is set."""
    from .history import HistoryDB
    h = HistoryDB()
    try:
        stored = h.get_meta(f"cfb_cookie_{source}")
    finally:
        h.close()
    return stored or os.environ.get(SOURCES[source]["cookie_env"])


def set_cookie(source, value):
    """Store/replace a source's cookie (from the GUI 'Nouveaux cookies' button). Clears the
    remembered 'expired' status so the next check re-evaluates fresh."""
    from .history import HistoryDB
    h = HistoryDB()
    try:
        h.set_meta(f"cfb_cookie_{source}", (value or "").strip())
        h.set_meta(f"cfb_status_{source}", "unknown")
    finally:
        h.close()


def _cookie(source):
    c = get_cookie(source)
    if not c:
        raise RuntimeError(f"aucun cookie CFB pour {source} — clique « Nouveaux cookies » "
                           f"dans l'interface (ou définis {SOURCES[source]['cookie_env']}).")
    return c


def _login_reason(final_url, body):
    """A reason string if the response looks like the login page, else None."""
    if re.search(r"/(login|sign[_-]?in|sessions|users/sign_in)\b", final_url or "", re.I):
        return "redirigé vers login"
    has_report = re.search(r"inventory_vendor|vendor_report|batch", body or "", re.I)
    has_pw = re.search(r'<input[^>]+type=["\']?password', body or "", re.I)
    if has_pw and not has_report:
        return "formulaire de login détecté"
    return None


# A real browser UA (some portals/WAFs treat unknown agents as anonymous → login page).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def _seed_jar(jar, cookie_str, host):
    """Load a 'name=value; name2=value2' cookie header into a CookieJar for `host`."""
    for part in cookie_str.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not name:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=host, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True, secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={}))


def _session(source):
    """A urllib opener with a cookie jar seeded from the source's cookie env var. The jar
    persists Set-Cookie across the remember_me→session redirect (the whole reason a valid
    cookie otherwise looked expired), and carries the session cookie from GET /new to POST."""
    host = urllib.parse.urlparse(SOURCES[source]["base"]).hostname
    jar = http.cookiejar.CookieJar()
    _seed_jar(jar, _cookie(source), host)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _request(opener, source, path, data=None, timeout=60):
    """GET/POST via `opener` (cookie jar attached). Returns (text, final_url, status). Does
    NOT judge auth — callers decide (so a dump can save even a login page for inspection)."""
    base = SOURCES[source]["base"]
    url = path if path.startswith("http") else base + path
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if data is not None else "GET")
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "fr-CA,fr;q=0.9,en;q=0.8")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.geturl(), resp.status


def _fetch(opener, source, path, data=None, timeout=60):
    """GET/POST + auth check. Returns (html, final_url); raises CfbAuthError on login bounce."""
    text, final_url, status = _request(opener, source, path, data, timeout)
    reason = _login_reason(final_url, text)
    if reason or status in (401, 403):
        raise CfbAuthError(source, reason or f"HTTP {status}")
    return text, final_url


class _FormParser(HTMLParser):
    """Collect the CSRF token, the form action, and every named input/select default —
    the same set boxoffice's loadReportForm posts back."""
    def __init__(self):
        super().__init__()
        self.action = None
        self.csrf = None
        self.meta_csrf = None
        self.fields = {}
        self._select_name = None
        self._select_first = None
        self._select_selected = None
        self._opt_open_value = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "meta" and a.get("name") == "csrf-token":
            self.meta_csrf = a.get("content")
        elif tag == "form":
            act = a.get("action", "")
            if act and (self.action is None or re.search(r"batch|vendor", act, re.I)):
                self.action = act
        elif tag == "input":
            name = a.get("name")
            if not name:
                return
            if name == "_csrf_token":
                self.csrf = a.get("value")
            typ = a.get("type", "").lower()
            if typ in ("checkbox", "radio"):
                if "checked" not in a:
                    return
                self.fields.setdefault(name, a.get("value", "on"))
            elif typ in ("submit", "button", "image"):
                return
            else:
                self.fields.setdefault(name, a.get("value", ""))
        elif tag == "select":
            self._select_name = a.get("name")
            self._select_first = self._select_selected = None
        elif tag == "option" and self._select_name:
            self._opt_open_value = a.get("value", None)
            if self._select_first is None:
                self._select_first = self._opt_open_value
            if "selected" in a:
                self._select_selected = self._opt_open_value

    def handle_endtag(self, tag):
        if tag == "select" and self._select_name:
            val = self._select_selected if self._select_selected is not None else self._select_first
            if val is not None:
                self.fields.setdefault(self._select_name, val)
            self._select_name = None


def load_form(source, opener=None):
    """GET /new and return (csrf, action, fields, html). Raises if no CSRF found."""
    opener = opener or _session(source)
    text, _ = _fetch(opener, source, NEW_PATH)
    p = _FormParser()
    p.feed(text)
    csrf = p.csrf or p.meta_csrf
    if not csrf:
        raise RuntimeError(f"CFB[{source}] : jeton CSRF introuvable sur /new")
    action = p.action or POST_PATH
    return csrf, action, p.fields, text


def vendor_options(form_html):
    """[(value, label), …] for the report form's vendor dropdown."""
    m = re.search(r'<select[^>]*name="[^"]*vendor_id[^"]*"[^>]*>(.*?)</select>',
                  form_html, re.S | re.I)
    if not m:
        return []
    out = []
    for om in re.finditer(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S):
        out.append((om.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", om.group(2))).strip()))
    return out


def match_vendor(want, options):
    """vendor_id whose label matches `want` — exact, or either-way substring so a mangled
    paste ('Binopython') still resolves to 'Bino'. None if nothing matches."""
    w = (want or "").lower()
    for v, label in options:
        lo = label.lower()
        if w == lo or w in lo or lo in w:
            return v
    return None


def candidate_labels(vendor, source):
    """The dropdown labels acceptable for canonical `vendor` on `source`'s portal. Honours
    config.CFB_VENDOR_SOURCE_ALIASES (e.g. MLJQ is listed as 'MLJC' on the US portal while
    Sylvain fixes it — we accept both so selection is idempotent to that rename). The
    canonical name is always included and tried first."""
    from . import config
    out = [vendor]
    aliases = getattr(config, "CFB_VENDOR_SOURCE_ALIASES", {})
    for name, per_src in aliases.items():
        if (vendor or "").lower() == (name or "").lower():
            for lbl in per_src.get(source, []):
                if lbl not in out:
                    out.append(lbl)
    return out


def match_vendor_source(vendor, source, options):
    """vendor_id for canonical `vendor` on `source`'s portal — tries each candidate label
    (canonical name + any per-source aliases) in order; None if none are present. Use this
    instead of match_vendor wherever a portal's `source` is known, so one CFB_VENDOR resolves
    correctly on portals that label the same seller differently (CA 'MLJQ' vs US 'MLJC')."""
    for want in candidate_labels(vendor, source):
        v = match_vendor(want, options)
        if v is not None:
            return v
    return None


def generate_report(source, opener=None, vendor=None, report_date=None):
    """Submit the batch report form (same session as the GET) and return the report HTML.
    `vendor` selects the seller by a case-insensitive label match in the vendor dropdown;
    if it isn't found, raises a clear error listing the available vendors (e.g. before the
    MLJQ seller exists, only 'Bino' is available). None = the form's default vendor.
    `report_date` (yyyy-mm-dd) sets the form's date → the inventory AS OF that date, so a
    month-end value can be read regardless of when the job actually runs."""
    opener = opener or _session(source)
    csrf, action, fields, form_html = load_form(source, opener)
    fields = dict(fields)
    fields["_csrf_token"] = csrf
    if report_date:
        dfield = next((k for k in fields if k.endswith("[date]") or k == "date"), None)
        if dfield:
            fields[dfield] = report_date
    if vendor:
        opts = vendor_options(form_html)
        match = match_vendor_source(vendor, source, opts)
        if match is None:
            raise VendorNotFound(source, vendor, [label for _, label in opts])
        vfield = next((k for k in fields if "vendor_id" in k), None)
        if vfield:
            fields[vfield] = match
    html_text, _ = _fetch(opener, source, action, data=fields)
    return html_text


# --- parsing -----------------------------------------------------------------
_MONEY = re.compile(r"(\d{1,3}(?:[  ,]\d{3})*(?:[.,]\d{2})?)")


def _to_float(s):
    s = s.replace(" ", "").replace(" ", "")
    # French formatting: thousands sep space/comma already stripped; decimal comma -> dot
    if "," in s and "." in s:
        s = s.replace(",", "")            # 1,234.56 -> 1234.56
    elif "," in s:
        s = s.replace(",", ".")           # 1234,56 -> 1234.56
    try:
        return float(s)
    except ValueError:
        return None


def _cell_int(s):
    d = re.sub(r"[^\d]", "", s or "")
    return int(d) if d else None


def parse_batch_totals(report_html):
    """Read the batch report's grand totals from its <tfoot> row — the summary the portal
    itself computes. Columns: Date | Lot ID | Source | Type | Item No | Color | Cond |
    Qty Added | Qty Sold (x%) | Qty Remaining (y%) | Actual Lot Price | Total Actual Value.
    Returns {'value': float, 'pieces': int, 'lots': int, 'as_of': 'yyyy-mm-dd',
    'vendor': str}. `pieces` = Qty Remaining (still in inventory); `value` = Total Actual
    Value (selling value)."""
    t = html.unescape(report_html)
    tf = re.search(r"<tfoot>(.*?)</tfoot>", t, re.S)
    scope = tf.group(1) if tf else t
    cells = [re.sub(r"<[^>]+>", " ", c).strip()
             for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", scope, re.S)]

    value = None
    for c in cells:                                  # last $ figure in the tfoot = the total
        m = re.search(r"\$\s*([\d,]+\.\d{2})", c)
        if m:
            value = float(m.group(1).replace(",", ""))
    pieces = None
    for c in cells:                                  # 'N (xx%)' cells are Sold then Remaining;
        m = re.match(r"^([\d,\s]+)\(([\d.]+)%\)$", c)  # keep the LAST → Qty Remaining
        if m:
            pieces = _cell_int(m.group(1))           # only the leading integer, not the %
    lots = None
    for c in cells:
        m = re.match(r"^([\d,\s]+)\s*Lots?$", c, re.I)
        if m:
            lots = _cell_int(m.group(1))

    as_of = re.search(r"as of\s*(\d{4}-\d{2}-\d{2})", t)
    vendor = re.search(r"<h3>\s*([^<$][^<]*?)\s*</h3>\s*<h4", t)
    return {
        "value": value, "pieces": pieces, "lots": lots,
        "as_of": as_of.group(1) if as_of else None,
        "vendor": vendor.group(1).strip() if vendor else None,
    }


def fetch_inventory_value(sources=("CA", "US"), vendor=None, report_date=None):
    """Read every source's batch report for `vendor` (default config.CFB_VENDOR) and return
    per-source + summed totals: {'per_source': {CA:{value,pieces,lots,…}, US:{…}}, 'value',
    'pieces', 'lots', 'as_of', 'vendor'}. Raises CfbAuthError (with .source) if a cookie is
    expired — a bad run is never a false 0 (that's how boxoffice once lost a day)."""
    if vendor is None:
        from . import config
        vendor = config.CFB_VENDOR
    from . import config as _cfg
    per = {}
    tot_v = tot_p = tot_l = 0.0
    as_of = None
    for s in sources:
        t = parse_batch_totals(generate_report(s, vendor=vendor, report_date=report_date))
        if s == "US" and t.get("value"):          # US portal reports USD → convert to CAD
            from . import fx as _fx
            rate, _rd, _src = _fx.get_usd_cad_rate(t.get("as_of"))   # BoC rate at as-of date
            t["value_usd"] = t["value"]
            t["value"] = round(t["value"] * rate, 2)
            t["fx"] = rate
        per[s] = t
        tot_v += t.get("value") or 0.0
        tot_p += t.get("pieces") or 0
        tot_l += t.get("lots") or 0
        as_of = as_of or t.get("as_of")
    return {"per_source": per, "value": round(tot_v, 2), "pieces": int(tot_p),
            "lots": int(tot_l), "as_of": as_of, "vendor": vendor}


def check_session(source):
    """Cheap liveness probe: GET the report form. {'source','ok',bool 'expired',err}. Never
    raises (so both sources can be reported)."""
    try:
        load_form(source)
        return {"source": source, "ok": True, "expired": False, "error": None}
    except CfbAuthError as e:
        return {"source": source, "ok": False, "expired": True, "error": str(e)}
    except Exception as e:
        return {"source": source, "ok": False, "expired": False, "error": str(e)}


def check_and_alert(sources=("CA", "US"), log=print, email=True):
    """Check each source's cookie and email ONCE when it flips to expired (status tracked in
    DB meta, so a daily job doesn't spam). Returns the list of per-source results. Meant to
    run daily; the GUI 'Nouveaux cookies' button resets status so a fresh cookie re-arms it."""
    from .history import HistoryDB
    from . import sheet
    h = HistoryDB()
    results, newly_bad = [], []
    try:
        for s in sources:
            r = check_session(s)
            results.append(r)
            status = "ok" if r["ok"] else "expired"
            prev = h.get_meta(f"cfb_status_{s}")
            h.set_meta(f"cfb_status_{s}", status)
            if status == "expired" and prev != "expired":
                newly_bad.append(s)
            log(f"  Cookie CFB {s} : "
                + ("OK" if r["ok"] else f"EXPIRÉ — {(r['error'] or '')[:70]}"))
    finally:
        h.close()
    if email and newly_bad:
        names = ", ".join(newly_bad)
        try:
            sheet.send_alert(
                f"Cookie CFB expiré ({names})",
                f"Le(s) cookie(s) CFB suivant(s) ne fonctionnent plus : {names}.\n\n"
                f"Ouvre l'interface MLJQ et clique « Nouveaux cookies » pour coller les "
                f"nouveaux (depuis le navigateur, portail mocs/usmocs.canadafirstbricks.com).")
            log(f"  Courriel d'alerte envoyé pour : {names}")
        except Exception as e:
            log(f"  ⚠ alerte courriel non envoyée : {str(e)[:80]}")
    return results


def _vendor_config(vendor):
    """The CFB_INVENTORY_VENDORS entry matching `vendor` (case-insensitive), or a default
    (financial col W + Sommaire mensuel) for a vendor not listed there."""
    from . import config
    for v in config.CFB_INVENTORY_VENDORS:
        if (v.get("vendor") or "").lower() == (vendor or "").lower():
            return v
    return {"vendor": vendor, "fin_col": 23, "sommaire": True}


def _capture_one(vendor_cfg, post, month, log):
    """Capture ONE seller (a CFB_INVENTORY_VENDORS entry): fetch its inventory value, store
    the dated snapshot, and (if post) write value/pieces to that seller's own column in the
    financial 'Résultats mensuels' sheet (`fin_col`), optionally also the accounting
    'Sommaire mensuel' B/D (`sommaire`). Returns the result dict, or None if the seller isn't
    on the portal yet (logged and skipped so a multi-vendor run keeps going). CfbAuthError
    (dead cookie) still propagates — a bad run is never a false 0."""
    vendor = vendor_cfg["vendor"]
    fin_col = vendor_cfg.get("fin_col", 23)
    sommaire = vendor_cfg.get("sommaire", True)
    # a target month reads the inventory AS OF that month's last day, so a late run (laptop
    # opened the 3rd/4th) still records the month-end value, not "today".
    report_date = _last_day_of_month(month) if month else None
    try:
        res = fetch_inventory_value(vendor=vendor, report_date=report_date)
    except VendorNotFound as e:
        log(f"Inventaire CFB « {vendor} » ignoré (pas encore un vendeur du portail) — {e}")
        return None
    from .history import HistoryDB
    h = HistoryDB()
    h.record_inventory_value(res, vendor=vendor)
    h.close()
    ca_v = res["per_source"].get("CA", {}).get("value") or 0
    us_v = res["per_source"].get("US", {}).get("value") or 0
    log(f"Inventaire CFB « {res.get('vendor') or vendor} » (au {res['as_of']}) : "
        f"{res['value']:,.2f}$ · {res['pieces']:,} pièces · {res['lots']:,} lots "
        f"(CA {ca_v:,.0f}$ + US {us_v:,.0f}$).")
    # Ce qui est chez nous et pas encore au portail. On l'AJOUTE a ce qui est poste, mais on
    # ne le melange pas a l'instantane range en base : celui-ci est une LECTURE du portail et
    # doit le rester, sinon on ne saurait plus ce que CFB detient vraiment.
    untreated_v = untreated_p = 0.0
    if vendor_cfg.get("untreated"):
        try:
            from . import untreated as _unt
            untreated_v, untreated_p, _rows = _unt.value()
        except Exception as e:
            log(f"  ⚠ inventaire non traité non calculé ({str(e)[:70]}) — valeur du portail seule.")
    res["untreated_value"] = untreated_v
    res["untreated_pieces"] = untreated_p
    res["posted_value"] = round(res["value"] + untreated_v, 2)
    res["posted_pieces"] = int(res["pieces"] + untreated_p)
    if untreated_v:
        log(f"  + non traité (chez nous, pas encore au portail) : {untreated_v:,.2f}$ · "
            f"{untreated_p:,} pièces → total posté {res['posted_value']:,.2f}$")

    if post:
        from . import sheet
        m = month or (res.get("as_of") or datetime.date.today().isoformat())[:7]
        reply = sheet.post_inventory_value(m, res["posted_value"], res["posted_pieces"],
                                           fin_col=fin_col, sommaire=sommaire, vendor=vendor)
        log(f"  → posté au sheet (mois {m}, col {fin_col}) : {reply}")
    return res


def capture(vendor=None, post=False, month=None, log=print):
    """Read each configured seller's CFB inventory value, store the dated snapshot, and (if
    post) write it to a month's rows. With no `vendor`, captures every seller in
    config.CFB_INVENTORY_VENDORS (Binobrick → financial col V; MLJQ → col W + Sommaire
    mensuel B/D); a seller not yet on the portal is skipped with a log line, not an error.
    Pass an explicit `vendor` (CLI --vendor) to capture only that one. `month` (yyyy-mm)
    chooses which row; default is the as-of (current) month — the monthly task passes the
    just-closed month so a 1st-of-month read lands in the prior month's row. Returns the list
    of per-vendor result dicts (skipped sellers omitted)."""
    from . import config
    targets = [_vendor_config(vendor)] if vendor is not None \
        else list(config.CFB_INVENTORY_VENDORS)
    results = []
    for cfg in targets:
        r = _capture_one(cfg, post=post, month=month, log=log)
        if r is not None:
            results.append(r)
    return results


def _prev_month_str(today=None):
    """'yyyy-mm' of the month before `today` (the just-closed month)."""
    today = today or datetime.date.today()
    y, m = today.year, today.month - 1
    if m < 1:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def _last_day_of_month(month):
    """'yyyy-mm' → 'yyyy-mm-dd' of that month's last day (the month-end as-of date)."""
    y, m = int(month[:4]), int(month[5:7])
    nxt = datetime.date(y + (m == 12), (m % 12) + 1, 1)
    return (nxt - datetime.timedelta(days=1)).isoformat()


# --- CLI: dump raw HTML so the parser can be finalised -----------------------
def _diagnose(text, final_url, status):
    """Human-readable read on what a page actually is (login vs real form)."""
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    title = html.unescape(title.group(1).strip()) if title else "(sans titre)"
    return {
        "status": status,
        "final_url": final_url,
        "title": title[:80],
        "len": len(text),
        "has_password_field": bool(re.search(r'<input[^>]+type=["\']?password', text, re.I)),
        "has_csrf": bool(re.search(r'name=["\']_csrf_token["\']|name=["\']csrf-token["\']', text, re.I)),
        "has_batch_words": bool(re.search(r"batch|inventory_vendor|vendor_report|inventaire", text, re.I)),
        "login_reason": _login_reason(final_url, text),
    }


def _dump(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for s in SOURCES:
        try:
            opener = _session(s)
            # Always save the raw /new response, even if it looks like a login page, so we
            # can see WHAT came back rather than guessing from the auth heuristic.
            text, final_url, status = _request(opener, s, NEW_PATH)
            path = os.path.join(out_dir, f"cfb_new_{s}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            diag = _diagnose(text, final_url, status)
            print(f"[{s}] /new -> {path}")
            for k, v in diag.items():
                print(f"       {k}: {v}")
            if diag["login_reason"] is None:      # looks real → try generating the report
                report = generate_report(s, opener)
                rp = os.path.join(out_dir, f"cfb_report_{s}.html")
                with open(rp, "w", encoding="utf-8") as f:
                    f.write(report)
                print(f"       rapport -> {rp}  heuristique: {parse_batch_totals(report)}")
        except Exception as e:
            print(f"[{s}] ERREUR: {type(e).__name__}: {str(e)[:200]}")
    print(f"\nFichiers dans : {out_dir}")
    print("Regarde le diagnostic ci-dessus. Si 'title' = page de login / 'has_password_field'"
          " True et 'has_batch_words' False → le cookie n'authentifie pas cette requête.")


if __name__ == "__main__":
    try:                              # accented/→ log lines must not crash a cp1252 console
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # optional: --vendor NAME  (else config.CFB_VENDOR)
    _vendor = None
    if "--vendor" in sys.argv:
        _i = sys.argv.index("--vendor")
        _vendor = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else None
    if "--dump" in sys.argv:
        _dump(os.path.join(os.getcwd(), "_cfb_debug"))
    elif "--check" in sys.argv:
        check_and_alert(log=print)
    elif "--store" in sys.argv:
        _month = _prev_month_str() if "--prev-month" in sys.argv else None
        capture(vendor=_vendor, post=("--post" in sys.argv), month=_month)
    else:
        import json
        print(json.dumps(fetch_inventory_value(vendor=_vendor), indent=2, ensure_ascii=False))
