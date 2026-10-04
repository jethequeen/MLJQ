# -*- coding: utf-8 -*-
"""
Our own SQLite of dated snapshots — the one thing BrickStore's blob can't give us:
history. Two tables:

  set_value   one row per (snapshot_date, set_id): the part-out value of a set on a
              given day, including recovery_value (flat realization) and weighted_ratio
              (sell velocity) that drive the "what to buy" decision.
  part_price  one row per (price_date, part): the full price guide for a part, keyed
              by the BrickStore price-cache date, so per-part prices can be tracked
              over time.

We never copy the catalog itself here (the blob is the catalog DB, queried live);
this file is purely the time series. Idempotent: re-running for the same date replaces
that date's rows rather than duplicating them.
"""

import os
import sqlite3
import datetime

from . import config
from . import setvalue
from .priceguide import METRIC_ORDER, parse_key

_SET_METRIC_COLS = METRIC_ORDER + ["recovery_value", "weighted_ratio"]

# buy_list column order (matches snapshot_buy_list's value tuple).
# `pieces` = les pieces du LOT (pieces du set x exemplaires), extras compris, comme la
# reference de verify et des batchs. C'est ce qui remplit un envoi, donc c'est ce chiffre
# qu'on garde ; les pieces par exemplaire se retrouvent en divisant par qty.
_BUY_COLS = ["run_date", "rank", "set_id", "name", "theme", "year", "source", "qty",
             "lot_cost_cad", "annual_profit_total_cad", "price_cad", "discount", "roi",
             "months", "annual_return_pct", "annual_profit_cad", "net_recovery_cad",
             "max_buy_cad", "coverage", "chosen", "url", "pieces"]
# Columns (and order) posted to the Google Sheet — the header row on the Achats tab.
# (theme + chosen dropped: theme isn't useful and only the chosen batch is posted.)
BUY_SHEET_COLS = ["rank", "set_id", "name", "year", "source", "qty", "price_cad",
                  "lot_cost_cad", "discount", "roi", "annual_return_pct",
                  "annual_profit_cad", "annual_profit_total_cad", "months", "url", "pieces"]


class HistoryDB:
    def __init__(self, path=None):
        self.path = path or config.HISTORY_DB
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._create()
        self._migrate()

    def _migrate(self):
        """Add columns introduced after a DB was first created."""
        have = {r[1] for r in self._conn.execute("PRAGMA table_info(set_value)")}
        for col in _SET_METRIC_COLS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE set_value ADD COLUMN {col} REAL")
        # buy_list: `source` must be part of the PRIMARY KEY so a set sold at BOTH stores
        # keeps one real row per store (no merge). Old tables had PK (run_date, set_id) —
        # rebuild them (buy_list is regenerated every run, so nothing of value is lost).
        info = self._conn.execute("PRAGMA table_info(buy_list)").fetchall()
        if info:
            pk_cols = {r[1] for r in info if r[5]}     # r[5] = pk position (>0 if in PK)
            if "source" not in pk_cols:
                self._conn.execute("DROP TABLE buy_list")
                self._create()                          # recreates buy_list with new PK
        # `url` (buy-page link) added later — add it wherever it's missing.
        for tbl in ("amazon_price", "costco_price", "buy_list"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({tbl})")}
            if cols and "url" not in cols:
                self._conn.execute(f"ALTER TABLE {tbl} ADD COLUMN url TEXT")
        # buy_list qty (copies to buy); bought amount (pre-tax spend).
        blc = {r[1] for r in self._conn.execute("PRAGMA table_info(buy_list)")}
        if blc and "qty" not in blc:
            self._conn.execute("ALTER TABLE buy_list ADD COLUMN qty INTEGER")
        if blc and "annual_profit_total_cad" not in blc:
            self._conn.execute("ALTER TABLE buy_list ADD COLUMN annual_profit_total_cad REAL")
        if "pieces" not in blc:
            self._conn.execute("ALTER TABLE buy_list ADD COLUMN pieces INTEGER")
        if blc and "lot_cost_cad" not in blc:
            self._conn.execute("ALTER TABLE buy_list ADD COLUMN lot_cost_cad REAL")
        bc = {r[1] for r in self._conn.execute("PRAGMA table_info(bought)")}
        if bc and "amount" not in bc:
            self._conn.execute("ALTER TABLE bought ADD COLUMN amount REAL")
        if bc and "src" not in bc:
            # Existing rows predate reconcile: keep them as 'manual' so a Journal re-sync
            # never deletes them (a real Journal row is re-tagged 'journal' on next sync).
            self._conn.execute("ALTER TABLE bought ADD COLUMN src TEXT DEFAULT 'manual'")
            self._conn.execute("UPDATE bought SET src='manual' WHERE src IS NULL")
        # bonus set(s) bundled with a Costco offer (comma-joined canonical ids, e.g. 75389-1)
        for tbl in ("amazon_price", "costco_price"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({tbl})")}
            if cols and "bonus" not in cols:
                self._conn.execute(f"ALTER TABLE {tbl} ADD COLUMN bonus TEXT")
        # inventory_value: `vendor` joins the PRIMARY KEY so two sellers (Binobrick, MLJQ)
        # can each keep a snapshot for the same as-of date. Old tables had PK (as_of) with no
        # vendor column — rebuild once, tagging the existing (smoke-test) rows as 'Bino'.
        iv = {r[1] for r in self._conn.execute("PRAGMA table_info(inventory_value)")}
        if iv and "vendor" not in iv:
            self._conn.executescript("""
                ALTER TABLE inventory_value RENAME TO inventory_value_old;
                CREATE TABLE inventory_value (
                    as_of        TEXT NOT NULL,
                    vendor       TEXT NOT NULL DEFAULT 'Bino',
                    ca_value     REAL, ca_pieces INTEGER, ca_lots INTEGER,
                    us_value     REAL, us_pieces INTEGER, us_lots INTEGER,
                    total_value  REAL, total_pieces INTEGER, total_lots INTEGER,
                    recorded_at  TEXT,
                    PRIMARY KEY (as_of, vendor)
                );
                INSERT INTO inventory_value
                    (as_of, vendor, ca_value, ca_pieces, ca_lots, us_value, us_pieces,
                     us_lots, total_value, total_pieces, total_lots, recorded_at)
                SELECT as_of, 'Bino', ca_value, ca_pieces, ca_lots, us_value, us_pieces,
                     us_lots, total_value, total_pieces, total_lots, recorded_at
                FROM inventory_value_old;
                DROP TABLE inventory_value_old;
            """)
        self._conn.commit()

    def _create(self):
        set_cols = ",\n            ".join(f"{c} REAL" for c in _SET_METRIC_COLS)
        price_cols = ",\n            ".join(f"{c} REAL" for c in METRIC_ORDER)
        self._conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS set_value (
            snapshot_date TEXT NOT NULL,
            set_id        TEXT NOT NULL,
            name          TEXT,
            year_from     INTEGER,
            lots          INTEGER,
            qty           INTEGER,
            coverage      REAL,
            price_date    TEXT,
            {set_cols},
            PRIMARY KEY (snapshot_date, set_id)
        );
        CREATE TABLE IF NOT EXISTS part_price (
            price_date        TEXT NOT NULL,
            item_type         TEXT NOT NULL,
            item_id           TEXT NOT NULL,
            color_id          INTEGER NOT NULL,
            current_qty_new   INTEGER,
            past_six_qty_new  INTEGER,
            {price_cols},
            PRIMARY KEY (price_date, item_type, item_id, color_id)
        );
        CREATE TABLE IF NOT EXISTS amazon_price (
            scrape_date TEXT NOT NULL,
            set_id      TEXT NOT NULL,
            theme       TEXT,
            price_cad   REAL,
            discount    TEXT,
            url         TEXT,
            bonus       TEXT,
            PRIMARY KEY (scrape_date, set_id)
        );
        CREATE TABLE IF NOT EXISTS costco_price (
            scrape_date TEXT NOT NULL,
            set_id      TEXT NOT NULL,
            theme       TEXT,
            price_cad   REAL,
            discount    TEXT,
            url         TEXT,
            bonus       TEXT,
            PRIMARY KEY (scrape_date, set_id)
        );
        CREATE TABLE IF NOT EXISTS buy_list (
            run_date          TEXT NOT NULL,
            rank              INTEGER,
            set_id            TEXT NOT NULL,
            name              TEXT,
            theme             TEXT,
            year              INTEGER,
            source            TEXT,
            qty               INTEGER,
            lot_cost_cad      REAL,
            annual_profit_total_cad REAL,
            price_cad         REAL,
            discount          TEXT,
            roi               REAL,
            months            REAL,
            annual_return_pct REAL,
            annual_profit_cad REAL,
            net_recovery_cad  REAL,
            max_buy_cad       REAL,
            coverage          REAL,
            chosen            INTEGER,
            url               TEXT,
            PRIMARY KEY (run_date, set_id, source)
        );
        CREATE TABLE IF NOT EXISTS bought (
            bought_date TEXT NOT NULL,
            set_id      TEXT NOT NULL,   -- canonical, e.g. 76342-1
            qty         INTEGER,
            vendor      TEXT,
            amount      REAL,            -- pre-tax CAD spent (Montant - TPS - TVQ)
            src         TEXT DEFAULT 'manual',  -- 'journal' (mirrors Journal) or 'manual'
            PRIMARY KEY (bought_date, set_id)
        );
        CREATE TABLE IF NOT EXISTS inventory (
            set_id TEXT PRIMARY KEY,      -- canonical; on-hand = bought - sent (>0 only)
            qty    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sent (
            sent_date TEXT NOT NULL,       -- when a set was processed & sent to CFB
            set_id    TEXT NOT NULL,       -- canonical
            qty       INTEGER NOT NULL     -- copies removed from stock (accumulates)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory_value (
            as_of        TEXT NOT NULL,       -- yyyy-mm-dd the CFB batch report is "as of"
            vendor       TEXT NOT NULL DEFAULT 'Bino',  -- which seller this snapshot is for
            ca_value     REAL, ca_pieces INTEGER, ca_lots INTEGER,
            us_value     REAL, us_pieces INTEGER, us_lots INTEGER,
            total_value  REAL, total_pieces INTEGER, total_lots INTEGER,
            recorded_at  TEXT,
            PRIMARY KEY (as_of, vendor)
        );
        CREATE TABLE IF NOT EXISTS monthly_budget (
            month         TEXT PRIMARY KEY,   -- 'yyyy-mm' the budget is FOR
            amount        REAL,               -- CAD to invest that month (delta × rate)
            delta         REAL,               -- net-worth delta of the source (prior) month
            rate          REAL,               -- MLJQ monthly-invest share used
            source_month  TEXT,               -- 'yyyy-mm' the delta came from (month - 1)
            computed_date TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_bought_set ON bought(set_id);
        CREATE INDEX IF NOT EXISTS ix_sent_set ON sent(set_id);
        CREATE INDEX IF NOT EXISTS ix_set_value_set ON set_value(set_id);
        CREATE INDEX IF NOT EXISTS ix_part_price_part
            ON part_price(item_type, item_id, color_id);
        CREATE INDEX IF NOT EXISTS ix_amazon_price_set ON amazon_price(set_id);
        """)
        self._conn.commit()

    # -- writing ----------------------------------------------------------
    def snapshot_set(self, v, snapshot_date=None):
        """Store one SetValue. snapshot_date defaults to today."""
        d = snapshot_date or datetime.date.today().isoformat()
        si = v.set_info
        cols = (["snapshot_date", "set_id", "name", "year_from", "lots", "qty",
                 "coverage", "price_date"] + _SET_METRIC_COLS)
        vals = [d, si.set_id if si else "", si.name if si else None,
                si.year_from if si else None, v.lots, v.total_qty,
                round(v.coverage, 4),
                datetime.date.fromtimestamp(v.price_mtime).isoformat()]
        vals += [round(v.totals[m], 4) for m in METRIC_ORDER]
        vals += [round(v.recovery_value, 4), round(v.weighted_ratio, 4)]
        self._conn.execute(
            f"INSERT OR REPLACE INTO set_value ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", vals)

    def snapshot_all_sets(self, cat, pg, snapshot_date=None, min_year=None,
                          progress=None):
        d = snapshot_date or datetime.date.today().isoformat()
        n = 0
        for set_no in cat.all_set_numbers():
            si = cat.set_info(set_no)
            if min_year and (not si.year_from or si.year_from < min_year):
                continue
            self.snapshot_set(setvalue.set_value(cat, pg, set_no), d)
            n += 1
            if progress and n % 500 == 0:
                progress(n)
        self._conn.commit()
        return n

    def snapshot_prices(self, pg, force=False, progress=None):
        """Dump the whole price guide, keyed by the cache's date. Skips if that date
        is already stored (unless force=True)."""
        price_date = datetime.date.fromtimestamp(pg.updated_mtime).isoformat()
        if not force:
            got = self._conn.execute(
                "SELECT 1 FROM part_price WHERE price_date=? LIMIT 1",
                (price_date,)).fetchone()
            if got:
                return 0, price_date
        cols = (["price_date", "item_type", "item_id", "color_id",
                 "current_qty_new", "past_six_qty_new"] + METRIC_ORDER)
        sql = (f"INSERT OR REPLACE INTO part_price ({','.join(cols)}) "
               f"VALUES ({','.join('?' * len(cols))})")
        n = 0
        for key, rec in pg.iter_all():
            pk = parse_key(key)
            if not pk:
                continue
            item_type, item_id, color_id = pk
            row = [price_date, item_type, item_id, color_id,
                   rec.current_qty_new, rec.past_six_qty_new]
            row += [round(getattr(rec, m), 4) for m in METRIC_ORDER]
            self._conn.execute(sql, row)
            n += 1
            if progress and n % 10000 == 0:
                progress(n)
        self._conn.commit()
        return n, price_date

    def snapshot_buy_list(self, cands, run_date=None):
        """Store the ranked buy list (already sorted by profit/$/year) for a run date.
        Replaces that date's rows. This is the single source of truth — no CSV."""
        d = run_date or datetime.date.today().isoformat()
        self._conn.execute("DELETE FROM buy_list WHERE run_date=?", (d,))
        self._conn.executemany(
            f"INSERT INTO buy_list ({','.join(_BUY_COLS)}) "
            f"VALUES ({','.join('?' * len(_BUY_COLS))})",
            [(d, i + 1, c.set_id, c.name, c.theme, c.year, c.source, c.qty,
              round(c.lot_cost, 2), round(c.annual_profit_total, 2), round(c.price, 2),
              c.discount, round(c.roi, 3), round(c.months, 1),
              round(c.annual_return * 100, 1), round(c.annual_profit, 2),
              round(c.net_recovery, 2), round(c.max_buy, 2), round(c.coverage, 3),
              1 if c.chosen else 0, c.url,
              int(getattr(c, "pieces", 0) or 0)) for i, c in enumerate(cands)])
        self._conn.commit()
        return len(cands)

    def snapshot_amazon(self, offers, scrape_date=None):
        return self._snapshot_feed("amazon_price", offers, scrape_date)

    def snapshot_costco(self, offers, scrape_date=None):
        return self._snapshot_feed("costco_price", offers, scrape_date)

    def _snapshot_feed(self, table, offers, scrape_date):
        """Store one scrape (list of Offer) into amazon_price/costco_price, keyed by
        date. Idempotent per date (INSERT OR REPLACE)."""
        d = scrape_date or datetime.date.today().isoformat()
        self._conn.executemany(
            f"INSERT OR REPLACE INTO {table} "
            f"(scrape_date, set_id, theme, price_cad, discount, url, bonus) "
            f"VALUES (?,?,?,?,?,?,?)",
            [(d, o.set_id, o.theme, round(o.price, 2), o.discount, getattr(o, "url", ""),
              getattr(o, "bonus", "") or "") for o in offers])
        self._conn.commit()
        return len(offers)

    def record_purchase(self, set_id, bought_date, qty, vendor, amount=None, src="manual"):
        """Log/replace one purchase (keyed by date+set). `amount` is the PRE-TAX CAD spent.
        `src='manual'` rows are never touched by a Journal re-sync. Recomputes inventory.
        Returns True if the row was NEW (vs. an update of an existing date+set)."""
        cur = self._conn.execute(
            "INSERT OR REPLACE INTO bought (bought_date, set_id, qty, vendor, amount, src) "
            "VALUES (?,?,?,?,?,?)",
            (bought_date, set_id, int(qty or 0), vendor, amount, src))
        self._recompute_inventory()
        self._conn.commit()
        return cur.rowcount > 0

    def reconcile_journal(self, rows):
        """Make the `bought` table a faithful MIRROR of the Journal's Catégorie-A rows so
        edits and deletions in the sheet are reflected (not just appends). `rows` is an
        iterable of (bought_date, set_id, qty, vendor, amount) parsed from the Journal.
        All existing src='journal' rows are dropped and replaced; src='manual' rows (hand
        entries, migrations) are preserved. Inventory is recomputed. Returns
        (n_journal_rows, added_ids) where added_ids is the set of canonical ids that were
        not present before (used to create CFB folders)."""
        rows = list(rows)
        before = {r[0] for r in self._conn.execute("SELECT DISTINCT set_id FROM bought")}
        self._conn.execute("DELETE FROM bought WHERE src='journal'")
        self._conn.executemany(
            "INSERT OR REPLACE INTO bought "
            "(bought_date, set_id, qty, vendor, amount, src) VALUES (?,?,?,?,?, 'journal')",
            [(d, sid, int(q or 0), vendor, amount) for (d, sid, q, vendor, amount) in rows])
        self._recompute_inventory()
        self._conn.commit()
        added = {sid for (_, sid, _, _, _) in rows} - before
        return len(rows), added

    def _recompute_inventory(self):
        """Rebuild on-hand stock = SUM(bought) − SUM(sent), per set, clamped at 0 and
        keeping only sets still in stock. `bought` (Journal mirror + manual) and `sent`
        (processed to CFB) are the two sources of truth; inventory is always derived, so
        a reconcile or a CFB send never fights a hand-edited inventory row."""
        self._conn.execute("DELETE FROM inventory")
        self._conn.execute(
            "INSERT INTO inventory (set_id, qty) "
            "SELECT b.set_id, MAX(0, b.q - COALESCE(s.q, 0)) "
            "FROM (SELECT set_id, SUM(qty) q FROM bought GROUP BY set_id) b "
            "LEFT JOIN (SELECT set_id, SUM(qty) q FROM sent GROUP BY set_id) s "
            "  ON s.set_id = b.set_id "
            "WHERE MAX(0, b.q - COALESCE(s.q, 0)) > 0")

    def send_to_cfb(self, set_no, sent_date=None):
        """Mark a set as processed and shipped to CFB: remove ALL on-hand copies from
        stock by recording a `sent` row, then recompute inventory. Accepts a bare number
        or canonical id. Returns the quantity removed — 0 if the set has no stock (already
        processed / never bought), in which case nothing is written (safe to click twice).
        Reversible: delete the `sent` row to restore the stock."""
        row = self._conn.execute(
            "SELECT set_id, qty FROM inventory WHERE set_id = ? OR set_id LIKE ? "
            "ORDER BY qty DESC LIMIT 1", (set_no, set_no + "-%")).fetchone()
        if not row or row[1] <= 0:
            return 0
        canonical, on_hand = row[0], int(row[1])
        d = sent_date or datetime.date.today().isoformat()
        self._conn.execute(
            "INSERT INTO sent (sent_date, set_id, qty) VALUES (?,?,?)",
            (d, canonical, on_hand))
        self._recompute_inventory()
        self._conn.commit()
        return on_hand

    def delivered(self, limit=None):
        """[{set_id, sent_date, qty}, …] — les sets LIVRÉS, du plus récent au plus ancien.

        La table `sent` est la seule trace qui survit : au « Livré », le dossier du set part à
        la corbeille. C'est donc elle, et pas un balayage du disque, qui alimente l'onglet
        Historique. Un set livré en deux fois (rare, mais possible) a deux lignes — on les
        garde séparées, elles disent la vérité."""
        sql = "SELECT sent_date, set_id, qty FROM sent ORDER BY sent_date DESC, rowid DESC"
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [{"sent_date": d, "set_id": s, "qty": q}
                for d, s, q in self._conn.execute(sql)]

    def record_inventory_value(self, result, vendor=None):
        """Store one CFB inventory snapshot (from cfb_inventory.fetch_inventory_value),
        keyed by (as_of, vendor) so each seller keeps its own history (idempotent). `result`
        has per_source[CA|US] + totals; `vendor` defaults to result['vendor'] or 'Bino'."""
        per = result.get("per_source", {})
        ca, us = per.get("CA", {}), per.get("US", {})
        self._conn.execute(
            "INSERT OR REPLACE INTO inventory_value (as_of, vendor, ca_value, ca_pieces, "
            "ca_lots, us_value, us_pieces, us_lots, total_value, total_pieces, total_lots, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (result.get("as_of") or datetime.date.today().isoformat(),
             vendor or result.get("vendor") or "Bino",
             ca.get("value"), ca.get("pieces"), ca.get("lots"),
             us.get("value"), us.get("pieces"), us.get("lots"),
             result.get("value"), result.get("pieces"), result.get("lots"),
             datetime.datetime.now().isoformat(timespec="seconds")))
        self._conn.commit()

    def latest_inventory_value(self, vendor=None):
        """Most recent inventory snapshot as a dict, or None. Pass `vendor` to restrict to
        one seller; otherwise the latest across all sellers."""
        cols = ["as_of", "vendor", "ca_value", "ca_pieces", "ca_lots", "us_value",
                "us_pieces", "us_lots", "total_value", "total_pieces", "total_lots",
                "recorded_at"]
        sql = f"SELECT {','.join(cols)} FROM inventory_value "
        args = ()
        if vendor:
            sql += "WHERE vendor = ? "
            args = (vendor,)
        sql += "ORDER BY as_of DESC LIMIT 1"
        row = self._conn.execute(sql, args).fetchone()
        return dict(zip(cols, row)) if row else None

    def get_meta(self, key):
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, str(value)))
        self._conn.commit()

    def inventory_qty(self, set_no):
        """Current on-hand quantity for a set (the CFB ×Quantité multiplier). Accepts a
        bare number ('76342') or canonical id ('76342-1'). None if not in stock."""
        row = self._conn.execute(
            "SELECT qty FROM inventory WHERE set_id = ? OR set_id LIKE ? "
            "ORDER BY qty DESC LIMIT 1", (set_no, set_no + "-%")).fetchone()
        return row[0] if row else None

    def recent_bought_ids(self, within_months):
        """Canonical set ids with a purchase in the last `within_months` months."""
        cutoff = (datetime.date.today()
                  - datetime.timedelta(days=int(round(within_months * 30.44)))).isoformat()
        return {r[0] for r in self._conn.execute(
            "SELECT DISTINCT set_id FROM bought WHERE bought_date >= ?", (cutoff,))}

    def spent_since(self, anchor_month):
        """Total PRE-TAX CAD spent on purchases on/after anchor_month (yyyy-mm)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM bought WHERE bought_date >= ?",
            (anchor_month + "-01",)).fetchone()
        return row[0] or 0.0

    def set_monthly_budget(self, month, amount, delta, rate, source_month,
                           computed_date=None):
        """Store/refresh one month's investable budget (idempotent per month)."""
        self._conn.execute(
            "INSERT INTO monthly_budget "
            "(month, amount, delta, rate, source_month, computed_date) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(month) DO UPDATE SET "
            "amount=excluded.amount, delta=excluded.delta, rate=excluded.rate, "
            "source_month=excluded.source_month, computed_date=excluded.computed_date",
            (month, amount, delta, rate, source_month,
             computed_date or datetime.date.today().isoformat()))
        self._conn.commit()

    def available_budget(self, fallback_monthly, anchor_month, today=None):
        """(available, months_elapsed). available = the SUM of the stored per-month budgets
        from anchor_month..current (each = that month's net-worth-delta × MLJQ rate), minus
        spent_since(anchor); this gives rattrapage for free (unspent months roll forward).
        Falls back to a flat fallback_monthly × months_elapsed only when NO monthly budgets
        are stored yet (e.g. the financial sheet was unreachable). The anchor month counts
        as 1 for the elapsed count (>=1 even before the anchor)."""
        today = today or datetime.date.today()
        ay, am = int(anchor_month[:4]), int(anchor_month[5:7])
        months = max(1, (today.year - ay) * 12 + (today.month - am) + 1)
        cur = f"{today.year:04d}-{today.month:02d}"
        total, n = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM monthly_budget "
            "WHERE month >= ? AND month <= ?", (anchor_month, cur)).fetchone()
        if not n:
            total = fallback_monthly * months        # no dynamic budgets yet → flat
        return (total or 0.0) - self.spent_since(anchor_month), months

    # -- reading ----------------------------------------------------------
    def amazon_latest(self):
        return self._feed_latest("amazon_price")

    def costco_latest(self):
        return self._feed_latest("costco_price")

    def _feed_latest(self, table):
        """(set_id, theme, price_cad, discount, url, bonus) rows from the most recent
        scrape of a feed table. Empty list if never scraped."""
        d = self._conn.execute(f"SELECT MAX(scrape_date) FROM {table}").fetchone()[0]
        if not d:
            return []
        return self._conn.execute(
            f"SELECT set_id, theme, price_cad, discount, url, COALESCE(bonus,'') "
            f"FROM {table} WHERE scrape_date=?", (d,)).fetchall()

    def latest_feed(self):
        """Both feeds' latest rows, each tagged with its store. Rows are
        (set_id, theme, price_cad, discount, source, url, bonus). buylist.rank makes one
        candidate per real offer (no merge); `bonus` = comma-joined bundled set ids."""
        rows = []
        for src, table in (("amazon", "amazon_price"), ("costco", "costco_price")):
            for sid, theme, price, disc, url, bonus in self._feed_latest(table):
                rows.append((sid, theme, price, disc, src, url, bonus))
        return rows

    def previous_chosen_ids(self, before_date):
        """Chosen set ids from the most recent run STRICTLY BEFORE before_date, or None
        if there's no prior run (used to email only when the batch changes)."""
        d = self._conn.execute(
            "SELECT MAX(run_date) FROM buy_list WHERE run_date < ?", (before_date,)
        ).fetchone()[0]
        if not d:
            return None
        return {r[0] for r in self._conn.execute(
            "SELECT set_id FROM buy_list WHERE run_date=? AND chosen=1", (d,))}

    def latest_buy_list_date(self):
        """Run date (yyyy-mm-dd) of the most recent buy list, or None if never ranked."""
        return self._conn.execute("SELECT MAX(run_date) FROM buy_list").fetchone()[0]

    def monthly_budgets(self, since=None):
        """[(month, amount), …] ascending — the stored per-month investable budgets,
        optionally only from `since` (yyyy-mm) onward. Feeds the phone's Finances tab."""
        sql = "SELECT month, amount FROM monthly_budget "
        args = ()
        if since:
            sql += "WHERE month >= ? "
            args = (since,)
        return [(m, a) for m, a in self._conn.execute(sql + "ORDER BY month", args)]

    def latest_buy_list(self, chosen_only=False):
        """The most recent buy list as (BUY_SHEET_COLS) rows, ordered by rank. With
        chosen_only=True, only the monthly batch (chosen=1) — what the sheet POST uses."""
        d = self._conn.execute("SELECT MAX(run_date) FROM buy_list").fetchone()[0]
        if not d:
            return []
        cols = ",".join(BUY_SHEET_COLS)
        where = "run_date=?" + (" AND chosen=1" if chosen_only else "")
        return self._conn.execute(
            f"SELECT {cols} FROM buy_list WHERE {where} ORDER BY rank", (d,)).fetchall()

    def amazon_price_series(self, set_id):
        """[(scrape_date, price_cad, discount), …] oldest first, for one set."""
        return self._conn.execute(
            "SELECT scrape_date, price_cad, discount FROM amazon_price "
            "WHERE set_id=? ORDER BY scrape_date", (set_id,)).fetchall()

    def set_value_series(self, set_id):
        """[(snapshot_date, current_new_avg, recovery_value), …] oldest first."""
        return self._conn.execute(
            "SELECT snapshot_date, current_new_avg, recovery_value FROM set_value "
            "WHERE set_id=? ORDER BY snapshot_date", (set_id,)).fetchall()

    def part_price_series(self, item_type, item_id, color_id):
        """[(price_date, current_new_avg, current_new_qtyavg), …] oldest first."""
        return self._conn.execute(
            "SELECT price_date, current_new_avg, current_new_qtyavg FROM part_price "
            "WHERE item_type=? AND item_id=? AND color_id=? ORDER BY price_date",
            (item_type, item_id, color_id)).fetchall()

    def close(self):
        self._conn.commit()
        self._conn.close()
