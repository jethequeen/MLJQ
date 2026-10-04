# -*- coding: utf-8 -*-
"""
Read BrickStore's local catalog (database-v12, "BSDB" binary format) DIRECTLY —
no BrickStore running, no BSX/XML export, no pyautogui. This is the offline
"which set contains which parts" source.

The file is a chunk container (header [4b id][u32 version][u64 size], payload padded
to 16 bytes, then a 16-byte footer; little-endian throughout). Relevant chunks:

    CAT   categories record: id u32 (= BrickLink category id), name (str), + 4-byte tail
    COL   colors     record: id u32 (= BrickLink color id), name (str), + 104-byte tail
    TYPE  item types  record: id qint8 (letter P/M/S/…), name (str), flags, categories
    ITEM  items       record: id (str), name (str), 14 fixed bytes
                              (itemTypeIndex u16, defaultColorIndex u16, year_from u8,
                               year_to u8, weight f64), then 8 PooledArray vectors:
                              appears_in(4B), consists_of(8B), knownColors(2B),
                              categories(2B), relMatchIds(2B), dimensions(8B),
                              pccs(8B), alternateIds(1B).

A "consists_of" entry (the set's inventory) is a little-endian u64 bitfield:
    quantity   bits 0-11      itemIndex  bits 12-31   (-> ITEM array)
    colorIndex bits 32-43     extra bit 44   isAlt bit 45   altId 46-51   counterpart 52
colorIndex indexes the COL array — it is NOT the BrickLink color id (idx 66 -> BL 69).

Strings on disk are [u32 byteLength][bytes] (id=latin1, name=utf16-le); vectors are
[u32 elementCount][count x elemSize] with 0xFFFFFFFF meaning null/empty.

Parsing the whole catalog is ~0.8 s (one pass, ~210k items). Validated 2026-08-21:
every item and color lands byte-exact at its chunk end, and set 76342-1 resolves to
301 lots that all price against priceguide_cache.sqlite with zero misses.
"""

import struct
from . import config

_NULL = 0xFFFFFFFF
# element size (bytes) of the 8 trailing PooledArray vectors, in file order
_VEC_ELEM = (4, 8, 2, 2, 2, 8, 8, 1)
_CONSISTS = 1          # index of consists_of within _VEC_ELEM
_CATS = 3              # index of the categories vector (u16 indices into the CAT array)
_ITEM_FIXED = 14       # itemType u16 + defColor u16 + yFrom u8 + yTo u8 + weight f64
_COL_TAIL = 104        # fixed bytes after a color's name (ldraw ids, QColors, particles)
# Solved the way the TYPE flags were: the only tail that makes all 1218 records land
# byte-exact on the chunk end. Checked against the categories BrickStore itself writes into
# our .bsx (463 moules, 0 ecart) -- see sortpack/categories.py, which stays the fallback.
_CAT_TAIL = 4


def _pad16(n):
    return n + (16 - n % 16) % 16


_SHARED = None


def shared():
    """One CatalogDB per process. Constructing one parses the whole catalog blob (~3 s), and
    several passes in a row want the same data — the leftovers helper is written on every
    Apply, right after the verification has already built one."""
    global _SHARED
    if _SHARED is None:
        _SHARED = CatalogDB()
    return _SHARED


class Part:
    """One line of a set's inventory, resolved to BrickLink identifiers."""
    __slots__ = ("item_type", "item_id", "color_id", "color_name", "qty",
                 "is_extra", "is_alt", "is_counterpart", "item_name",
                 "category_id", "category_name")

    def __init__(self, item_type, item_id, color_id, color_name, qty,
                 is_extra, is_alt, is_counterpart, item_name="",
                 category_id="", category_name=""):
        self.item_type = item_type          # BrickLink letter: 'P', 'M', ...
        self.item_id = item_id
        self.color_id = color_id            # BrickLink color id
        self.color_name = color_name
        self.qty = qty
        self.is_extra = is_extra
        self.is_alt = is_alt
        self.is_counterpart = is_counterpart
        self.item_name = item_name          # catalog display name (for a written .bsx)
        self.category_id = category_id      # BrickLink category (Phase A sorts on it)
        self.category_name = category_name

    def price_key(self):
        """The key used by priceguide_cache.sqlite: e.g. 'P3001@5@B0'."""
        return f"{self.item_type}{self.item_id}@{self.color_id}@B0"

    def __repr__(self):
        return (f"Part({self.item_type}{self.item_id} c{self.color_id} "
                f"x{self.qty}{' extra' if self.is_extra else ''})")


class SetInfo:
    __slots__ = ("set_id", "name", "year_from", "year_to", "_index")

    def __init__(self, set_id, name, year_from, year_to, index):
        self.set_id = set_id                # full BrickLink id, e.g. '76342-1'
        self.name = name
        self.year_from = year_from
        self.year_to = year_to
        self._index = index                 # position in the ITEM array


class CatalogDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or config.BRICKSTORE_DB
        with open(self.db_path, "rb") as f:
            self._data = f.read()
        if self._data[:4] != b"BSDB":
            raise ValueError(f"Not a BSDB catalog file: {self.db_path!r}")
        self.version = struct.unpack_from("<I", self._data, 4)[0]

        self._chunks = self._index_chunks()
        self._cat_id = []        # catIndex -> BrickLink category id
        self._cat_name = []      # catIndex -> name
        self._color_id = []      # colorIndex -> BrickLink color id
        self._color_name = []    # colorIndex -> name
        self._type_letter = {}   # typeIndex -> letter
        self._item_id = []       # itemIndex -> item id (str)
        self._item_type = []     # itemIndex -> typeIndex
        self._item_name = []     # itemIndex -> (offset, byteLen) of the utf16 name
        self._item_years = []    # itemIndex -> (year_from, year_to)
        self._consists = []      # itemIndex -> (offset, count) of its consists_of
        self._item_cats = []     # itemIndex -> (offset, count) of its categories vector
        self._item_index = None  # ItemID -> itemIndex, construit a la demande
        self._sets = {}          # normalized set number -> itemIndex (the type-'S' one)

        self._parse_categories()
        self._parse_colors()
        self._parse_types()
        self._parse_items()

    # -- chunk directory --------------------------------------------------
    def _index_chunks(self):
        data = self._data
        out = {}
        pos, end = 16, 16 + struct.unpack_from("<Q", data, 8)[0]
        while pos + 16 <= end:
            cid = data[pos:pos + 4]
            size, = struct.unpack_from("<Q", data, pos + 8)
            out[cid] = (pos + 16, size)          # (payload offset, payload size)
            pos = pos + 16 + _pad16(size) + 16
        return out

    def _read_str(self, p, utf16):
        blen, = struct.unpack_from("<I", self._data, p)
        if blen == _NULL:
            return "", p + 4
        raw = self._data[p + 4:p + 4 + blen]
        s = raw.decode("utf-16-le" if utf16 else "latin1", "replace")
        return s, p + 4 + blen

    def _skip_str(self, p):
        """Advance past a [u32 byteLen][bytes] string without decoding it."""
        blen, = struct.unpack_from("<I", self._data, p)
        if blen == _NULL:
            return p + 4
        return p + 4 + blen

    def _decode_name(self, loc):
        off, blen = loc
        if blen == 0:
            return ""
        return self._data[off:off + blen].decode("utf-16-le", "replace")

    # -- COL --------------------------------------------------------------
    def _parse_colors(self):
        data = self._data
        payload, size = self._chunks[b"COL "]
        count, = struct.unpack_from("<I", data, payload)
        p = payload + 4
        for _ in range(count):
            cid, = struct.unpack_from("<I", data, p)
            name, p = self._read_str(p + 4, utf16=True)
            self._color_id.append(cid)
            self._color_name.append(name)
            p += _COL_TAIL

    # -- CAT --------------------------------------------------------------
    def _parse_categories(self):
        """La categorie d'un moule, que Phase A utilise pour trier (Type -> Categorie ->
        Description -> Couleur). Sans elle, un lot vu nulle part ailleurs se trie comme la
        chaine vide et part en tete -- ce qui decale tout le plan."""
        data = self._data
        payload, size = self._chunks[b"CAT "]
        count, = struct.unpack_from("<I", data, payload)
        p = payload + 4
        for _ in range(count):
            cid, = struct.unpack_from("<I", data, p)
            name, p = self._read_str(p + 4, utf16=True)
            self._cat_id.append(cid)
            self._cat_name.append(name)
            p += _CAT_TAIL

    # -- TYPE -------------------------------------------------------------
    def _parse_types(self):
        data = self._data
        payload, size = self._chunks[b"TYPE"]
        end = payload + size
        count, = struct.unpack_from("<I", data, payload)

        # The only unknown is the flags width; solve it against the "all types land
        # at payload end" oracle so we never hard-code a guess.
        def attempt(flags):
            p = payload + 4
            out = {}
            for i in range(count):
                if p + 5 > end:
                    return None
                tid = data[p]
                p += 1
                blen, = struct.unpack_from("<I", data, p)
                if blen == _NULL:
                    blen = 0
                if blen > 64:
                    return None
                p += 4 + blen + flags
                if p + 4 > end:
                    return None
                n, = struct.unpack_from("<I", data, p)
                p += 4
                if n == _NULL:
                    n = 0
                if n > 4000:
                    return None
                p += n * 2
                out[i] = chr(tid) if 32 <= tid < 127 else "?"
            return out if 0 <= end - p < 16 else None

        for flags in range(0, 16):
            res = attempt(flags)
            if res is not None:
                self._type_letter = res
                return
        raise ValueError("could not parse TYPE chunk (flags width not found)")

    # -- ITEM -------------------------------------------------------------
    def _parse_items(self):
        data = self._data
        payload, size = self._chunks[b"ITEM"]
        count, = struct.unpack_from("<I", data, payload)
        p = payload + 4
        for idx in range(count):
            item_id, p = self._read_str(p, utf16=False)
            name_off = p + 4                     # name bytes start (decoded lazily)
            name_len, = struct.unpack_from("<I", data, p)
            if name_len == _NULL:
                name_len = 0
            p = self._skip_str(p)
            type_index, = struct.unpack_from("<H", data, p)
            year_from = data[p + 4]
            year_to = data[p + 5]
            p += _ITEM_FIXED
            consists = (0, 0)
            cats = (0, 0)
            for v, esz in enumerate(_VEC_ELEM):
                n, = struct.unpack_from("<I", data, p)
                p += 4
                if n == _NULL:
                    n = 0
                if v == _CONSISTS:
                    consists = (p, n)
                elif v == _CATS:
                    cats = (p, n)
                p += n * esz
            self._item_id.append(item_id)
            self._item_type.append(type_index)
            self._item_name.append((name_off, name_len))
            self._item_years.append((year_from, year_to))
            self._consists.append(consists)
            self._item_cats.append(cats)
            if self._type_letter.get(type_index) == "S":
                self._sets[_norm_set(item_id)] = idx

    # -- public API -------------------------------------------------------
    def _resolve_set_index(self, set_no):
        idx = self._sets.get(_norm_set(set_no))
        if idx is None:
            # allow passing the bare number for a "-1" set
            idx = self._sets.get(_norm_set(f"{set_no}-1"))
        return idx

    def has_set(self, set_no):
        return self._resolve_set_index(set_no) is not None

    def set_info(self, set_no):
        idx = self._resolve_set_index(set_no)
        if idx is None:
            return None
        yf, yt = self._item_years[idx]
        return SetInfo(self._item_id[idx], self._decode_name(self._item_name[idx]),
                       _year(yf), _year(yt), idx)

    def inventory(self, set_no, include_extras=False,
                  include_counterparts=False, include_alternates=False):
        """Resolved list of Part for a set. Alternates/counterparts/extras are
        excluded by default (extras are BrickLink spare parts; counterparts and
        alternates are not standalone value)."""
        idx = self._resolve_set_index(set_no)
        if idx is None:
            raise KeyError(f"set {set_no!r} not found in catalog")
        off, n = self._consists[idx]
        data = self._data
        parts = []
        for k in range(n):
            w, = struct.unpack_from("<Q", data, off + k * 8)
            qty = w & 0xFFF
            item_index = (w >> 12) & 0xFFFFF
            color_index = (w >> 32) & 0xFFF
            is_extra = bool((w >> 44) & 1)
            is_alt = bool((w >> 45) & 1)
            is_cpart = bool((w >> 52) & 1)
            if is_alt and not include_alternates:
                continue
            if is_cpart and not include_counterparts:
                continue
            if is_extra and not include_extras:
                continue
            parts.append(Part(
                item_type=self._type_letter.get(self._item_type[item_index], "?"),
                item_id=self._item_id[item_index],
                color_id=self._color_id[color_index],
                color_name=self._color_name[color_index],
                qty=qty,
                is_extra=is_extra, is_alt=is_alt, is_counterpart=is_cpart,
                item_name=self._decode_name(self._item_name[item_index]),
                category_id=self.category_of_index(item_index)[0],
                category_name=self.category_of_index(item_index)[1]))
        return parts

    def item_index(self, item_id):
        """itemIndex d'un ItemID, ou None. Construit a la demande : la plupart des passes
        n'ont jamais besoin de chercher un moule par son id."""
        if self._item_index is None:
            self._item_index = {iid: i for i, iid in enumerate(self._item_id)}
        return self._item_index.get(str(item_id))

    def category_of_index(self, item_index):
        """(id, nom) de la categorie PRINCIPALE d'un moule — la premiere du vecteur, qui est
        celle que BrickStore ecrit dans un .bsx. ("", "") si le catalogue n'en donne pas."""
        off, n = self._item_cats[item_index]
        if not n:
            return ("", "")
        ci, = struct.unpack_from("<H", self._data, off)
        if ci >= len(self._cat_id):
            return ("", "")
        return (str(self._cat_id[ci]), self._cat_name[ci])

    def all_set_numbers(self):
        """Every normalized set number in the catalog (for the 'set of sets' report)."""
        return list(self._sets.keys())


def _norm_set(set_id):
    """Normalize a set id for lookup: keep the BrickLink '-N' variant but strip case
    and surrounding whitespace."""
    return set_id.strip().lower()


def _year(byte_val):
    """year_from / year_to are stored as an offset from 1900 in a byte (0 = unknown)."""
    return 1900 + byte_val if byte_val else None
