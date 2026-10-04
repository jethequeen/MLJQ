# -*- coding: utf-8 -*-
"""
Amazon (Canada) LEGO discounts, scraped from Brickset's buy list — fully headless
(plain HTTP + HTML parsing, no browser, no login). Ported from the reference
Automatisation/SetsEnRabais.py (the Amazon half, which works; the Costco half uses
Selenium and is out of scope / broken).

Brickset lists Amazon.ca offers at:
    https://brickset.com/buy/country-CA/vendor-amazon/order-percentdiscount
Each result row carries the full BrickLink-style set id (e.g. "10414-1"), theme,
current price (CAD) and discount. We parse row-by-row (robust) rather than by three
parallel column lists.
"""

import re

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://brickset.com/buy/country-CA/vendor-amazon/order-percentdiscount"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"),
    "Accept-Language": "en-CA, en;q=0.5",
}
_PER_PAGE = 50


class Offer:
    __slots__ = ("set_id", "theme", "price", "discount", "name", "year", "url")

    def __init__(self, set_id, theme, price, discount, name, year, url):
        self.set_id = set_id        # full id, e.g. "10414-1"
        self.theme = theme
        self.price = price          # CAD, float
        self.discount = discount    # e.g. "44%"
        self.name = name
        self.year = year
        self.url = url              # Amazon.ca product page


def _money(text):
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _parse_page(soup):
    offers = []
    for tr in soup.select("table tr"):
        hide = tr.find("td", class_="hideonsmallscreen")
        if not hide:
            continue
        s = list(hide.stripped_strings)          # [name, id, theme, subtheme, year, …]
        if len(s) < 2 or "-" not in s[1]:
            continue
        set_id = s[1]
        name = s[0]
        theme = s[2] if len(s) > 2 else ""
        year = next((int(x) for x in s[3:6] if x.isdigit() and len(x) == 4), None)
        disc_td = tr.find("td", class_="disc")
        discount = disc_td.get_text(strip=True) if disc_td else "0%"
        # price = first "$…" in the textcenter cell that isn't the thumbnail
        price = None
        for td in tr.find_all("td", class_="textcenter"):
            if "thumbnail" in (td.get("class") or []):
                continue
            price = _money(td.get_text(" ", strip=True))
            if price is not None:
                break
        if price is None:
            continue
        # buy link: the row's Amazon.ca product link (clean off the affiliate ref)
        url = ""
        a = tr.find("a", href=re.compile(r"amazon\.ca/dp/", re.I))
        if a:
            m = re.search(r"/dp/([A-Z0-9]{10})", a.get("href", ""))
            url = f"https://www.amazon.ca/dp/{m.group(1)}" if m else a.get("href", "")
        offers.append(Offer(set_id, theme, price, discount, name, year, url))
    return offers


def fetch(max_pages=40, timeout=30, session=None):
    """Return a list of Offer for every Amazon.ca LEGO discount on Brickset."""
    sess = session or requests.Session()
    first = sess.get(BASE_URL, headers=_HEADERS, timeout=timeout)
    first.raise_for_status()
    soup = BeautifulSoup(first.content, "html.parser")
    res = soup.find("div", class_="results")
    total = None
    if res:
        nums = re.findall(r"([\d,]+)", res.get_text())
        total = int(nums[-1].replace(",", "")) if nums else None
    pages = (total // _PER_PAGE + 1) if total else max_pages
    pages = min(pages, max_pages)

    offers = _parse_page(soup)                    # page 1 (already fetched)
    for page in range(2, pages + 1):
        r = sess.get(f"{BASE_URL}/page-{page}", headers=_HEADERS, timeout=timeout)
        if r.status_code != 200:
            break
        page_offers = _parse_page(BeautifulSoup(r.content, "html.parser"))
        if not page_offers:
            break
        offers.extend(page_offers)
    return offers
