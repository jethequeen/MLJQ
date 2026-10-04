# -*- coding: utf-8 -*-
"""
Costco.ca LEGO discounts — FULLY HEADLESS via Costco's own product API (no browser).

The website is a Next.js SPA behind Akamai that blocks headless browsers, but the JSON
API it calls (gdx-api.costco.com/catalog/search) needs only a few static headers and NO
cookies, so plain HTTP works. It returns the "Building Blocks & Sets → brand LEGO"
listing (~120 in-stock SKUs) with the set number in `product.attributes.model`, the
current price and the original price (for the discount). Paginated by offset.

Region-specific fields (postal/warehouse) are the user's own QC location so prices match
their local Costco. If Costco ever rotates CLIENT_IDENTIFIER, refresh it from a real
request (DevTools → the /catalog/search call → request headers).
"""

import re
import json
import uuid
import urllib.request

API_URL = "https://gdx-api.costco.com/catalog/search/api/v1/search"
CLIENT_IDENTIFIER = "168287ea-1201-45f6-9b45-5bbea49f8ee7"
# Region (from the user's own session): Quebec, warehouse 1359.
_SHIP_POSTAL, _SHIP_STATE, _WAREHOUSE = "J3N 1V1", "QC", "1359-wh"
_DELIVERY_LOCATIONS = ["894", "801-bd", "1359-wh", "559-dz", "559-wm", "792-wm",
                       "894_0-cwt", "894_0-edi", "894_0-membership", "894_0-mpt",
                       "894_0-otw", "894_0-spc", "894_1-edi", "894_1-mpt", "946-dz",
                       "946-wm", "9894-wcs", "993-wm"]
_HEADERS = {
    "content-type": "application/json",
    "client-identifier": CLIENT_IDENTIFIER,
    "client_id": "CABC",
    "searchresultprovider": "GRS",
    "locale": "en-CA",
    "accept-language": "en-CA",
    "referer": "https://www.costco.ca/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
}


class Offer:
    __slots__ = ("set_id", "theme", "price", "discount", "source", "name", "url", "bonus")

    def __init__(self, set_id, theme, price, discount, name, url, bonus=""):
        self.set_id = set_id
        self.theme = theme
        self.price = price          # CAD, float — current Costco price (whole bundle)
        self.discount = discount    # e.g. "20%" if on sale, else ""
        self.source = "costco"
        self.name = name
        self.url = url              # Costco.ca product page
        self.bonus = bonus          # comma-joined bonus set numbers bundled in (or "")


def _min_price(price_obj):
    """minPrice out of an inventoryResponse price object, as float or None."""
    if isinstance(price_obj, dict) and price_obj.get("minPrice") not in (None, ""):
        try:
            return float(price_obj["minPrice"])
        except (TypeError, ValueError):
            return None
    return None


def _theme(name):
    m = re.match(r"\s*LEGO\s+([A-Za-z][\w'&.-]*(?:\s+[A-Z][\w'&.-]*)?)", name or "")
    return m.group(1).strip() if m else ""


def _resolve_set(model_texts, title, is_set):
    """Pick the real set number: the API's model field first, else numbers in the
    title, validated against the catalog (is_set)."""
    cands = list(model_texts or [])
    cands += re.findall(r"\b(\d{4,7})\b", title or "")
    for c in cands:
        if is_set is None or is_set(c):
            return c
    return None


def _resolve_bundle(model_texts, title, is_set):
    """(main_set, [bonus_sets]) for a possible bundle. The authoritative `model` field can
    list several real sets (main first); a Costco "… with Bonus <NNNNN>" title names an
    extra set. Only numbers AFTER the word "Bonus" are trusted from the title, so
    name-numbers ('LEGO Icons 10305', a '1966' in a car name) aren't mistaken for a bonus.
    All are validated against the catalog and de-duplicated; the main set is excluded."""
    models = [c for c in (model_texts or []) if is_set is None or is_set(c)]
    main = models[0] if models else _resolve_set(model_texts, title, is_set)
    if not main:
        return None, []
    bonus = []
    for c in models[1:]:                       # extra authoritative models = real bundle
        if c != main and c not in bonus:
            bonus.append(c)
    parts = re.split(r"(?i)\bbonus\b", title or "", maxsplit=1)
    if len(parts) > 1:                         # numbers after "Bonus" in the title
        for c in re.findall(r"\b(\d{4,7})\b", parts[1]):
            if (is_set is None or is_set(c)) and c != main and c not in bonus:
                bonus.append(c)
    return main, bonus


def _post(offset, page_size, timeout):
    body = {
        "visitorId": uuid.uuid4().hex, "query": "", "pageSize": page_size,
        "offset": offset, "orderBy": None, "searchMode": "page",
        "personalizationEnabled": True, "warehouseId": _WAREHOUSE,
        "shipToPostal": _SHIP_POSTAL, "shipToState": _SHIP_STATE,
        "deliveryLocations": _DELIVERY_LOCATIONS,
        "filterBy": ['brands: ANY("LEGO")',
                     'attributes.category_uri: ANY("building-blocks-sets")',
                     "HIDE_OUT_OF_STOCK"],
        "pageCategories": ["building-blocks-sets"],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(is_set=None, page_size=100, max_pages=6, timeout=40):
    """Return a list of Offer for every in-stock Costco LEGO set. `is_set(set_no)`
    resolves the set number (pass CatalogDB.has_set). Plain HTTP — no browser."""
    offers = []
    seen = set()
    for page in range(max_pages):
        data = _post(page * page_size, page_size, timeout)
        results = (data.get("searchResult") or {}).get("results") or []
        if not results:
            break
        # authoritative prices live in inventoryResponse (real delivery/warehouse price,
        # eco fees in; the rollup price can be stale — e.g. $79 vs real $64.99).
        inv = {r.get("productId"): r for r in (data.get("inventoryResponse") or [])}
        for item in results:
            prod = item.get("product") or {}
            title = prod.get("title") or ""
            attrs = prod.get("attributes") or {}
            model = (attrs.get("model") or {}).get("text") or []
            set_id, bonus = _resolve_bundle(model, title, is_set)
            if not set_id or set_id in seen:
                continue
            rec = inv.get(item.get("id"), {})
            price = _min_price(rec.get("deliveryPrice")) or _min_price(rec.get("warehousePrice"))
            if price is None:                       # fallback to the rollup price
                roll = (item.get("variantRollupValues") or {}).get("price") or []
                price = float(roll[0]) if roll else None
            if not price:
                continue
            orig = _min_price(rec.get("originalPrice"))
            discount = ""
            if orig and orig > price > 0:
                discount = f"{round((1 - price / orig) * 100)}%"
            # buy link: the real product URL is embedded in the item JSON
            # (costco.ca/p/-/<slug>/<id>); fall back to the id form that redirects.
            m = re.search(r"https://www\.costco\.ca/p/-/[^\"\\ ]+", json.dumps(item))
            url = m.group(0) if m else f"https://www.costco.ca/.product.{item.get('id')}.html"
            seen.add(set_id)
            offers.append(Offer(set_id, _theme(title), price, discount, title, url,
                                bonus=",".join(bonus)))
        if len(results) < page_size:
            break
    return offers
