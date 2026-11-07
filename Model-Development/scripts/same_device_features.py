"""The shared feature function for the same-device work (Day 1 contract).

STATUS: bodies written (H1). Constants, signatures and FEATURE_COLUMNS are the Step 0 contract.

This file is the ONLY place in the repo where these rules may be written down. SQL files get them
through the `sql_*` emitters below (macros such as `{{ identifier_key(m.model_number_raw) }}`),
never as pasted regex literals. Rules: docs/SAME_DEVICE_DEFINITION.md section 4 and the README
"Shared conventions". No fuzzy matching (rapidfuzz, Jaro-Winkler) here: it cannot be written in
SQL, and one definition for both engines is the point.

Column namespaces for any table or CSV built from this module:
  key_*    join and audit columns (includes key_gudid_udi_di). Never given to a model.
  label, label_source
  feat_*   the ONLY columns a model sees (FEATURE_COLUMNS below).
  meta_*   split, device key, strata, duplicate counts, sample weights. Never given to a model.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

# --- Constants: the only copies in the repo ------------------------------------------------------
# Both regexes are valid in Python `re` and BigQuery RE2 exactly as written.
STRIP_CHARS_RE = r'[\s\-_./]'
PLACEHOLDER_RE = (r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|'
                  r'UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
MIN_KEY_LEN = 2                  # E6: a key under 2 characters is missing
NON_DISTINCTIVE_MAX_DIS = 20     # E4: more DIs than this for one (labeler, model key) = not distinctive. Open for sign-off.
SUFFIX_LENS = (1, 2, 3)          # E3: a sibling is the same key plus 1-3 trailing characters
ALIAS_MIN_DIS = 3                # E5: an alias pair needs this many distinct DIs. Open for sign-off.
TEST_SPLIT_PCT = 20              # share of devices in the test split
SPLIT_SALT = "v1"                # change it and the whole split changes: bump the label-set version too
KIT_WORD_RE = r'\b(KIT|KITS|SYSTEM|SYSTEMS|SET|TRAY|PACK)\b'   # same pattern as query 13
LEAK_MIN_SUBSTRING = 6           # a feat_* string this long that sits inside the DI is a derived leak

# --- Column contract -----------------------------------------------------------------------------
FEATURE_COLUMNS: tuple[str, ...] = (
    "feat_match_kind",
    "feat_identifier_exact",
    "feat_identifier_cross_field",
    "feat_raw_identifier_equal",
    "feat_token_contains",
    "feat_prefix_related",
    "feat_conflicting_identifier",
    "feat_distinctive",
    "feat_n_dis_for_model",
    "feat_labeler_equal",
    "feat_labeler_alias_equal",
    "feat_labeler_missing",
    "feat_product_code_agree",
    "feat_maude_model_missing",
    "feat_maude_catalog_missing",
    "feat_gudid_model_missing",
    "feat_device_name_token_overlap",
    "feat_maude_name_has_kit_word",
    "feat_gudid_name_has_kit_word",
)
# A feature column with one of these as a whole "_"-delimited token is a leak (checked by
# assert_no_leakage). Whole tokens, not substrings: "_di" would also hit feat_distinctive and
# feat_n_dis_for_model. "gudid" is NOT banned: feat_gudid_model_missing is a fine feature. This is the
# cheap name check; assert_no_value_leakage is the strong one.
BANNED_FEATURE_TOKENS = ("udi", "di", "pair_id", "label", "split", "device_key")


def constants() -> dict[str, Any]:
    """Every rule constant, for the label manifest and the golden-fixture header (hash it to detect drift)."""
    return {"STRIP_CHARS_RE": STRIP_CHARS_RE, "PLACEHOLDER_RE": PLACEHOLDER_RE, "MIN_KEY_LEN": MIN_KEY_LEN,
            "NON_DISTINCTIVE_MAX_DIS": NON_DISTINCTIVE_MAX_DIS, "SUFFIX_LENS": list(SUFFIX_LENS),
            "ALIAS_MIN_DIS": ALIAS_MIN_DIS, "TEST_SPLIT_PCT": TEST_SPLIT_PCT, "SPLIT_SALT": SPLIT_SALT,
            "KIT_WORD_RE": KIT_WORD_RE, "FEATURE_COLUMNS": list(FEATURE_COLUMNS)}


def is_banned_feature_name(name: str) -> bool:
    """True when `name` contains a banned token between underscores."""
    padded = f"_{name}_"
    return any(f"_{tok}_" in padded for tok in BANNED_FEATURE_TOKENS)


class LeakageError(ValueError):
    """A feature column or value would let a model see the UDI, the label or the split."""


class MatchKind(StrEnum):
    """How the identifiers of a MAUDE row and a GUDID record relate. First match wins.

    Same names and same order as the CASE in sql/same_device/08_udi_link_agreement.sql.
    """
    SAME_MODEL_KEY = "same_model_key"
    MAUDE_MODEL_EQ_GUDID_CATALOG = "maude_model_eq_gudid_catalog"
    MAUDE_CATALOG_EQ_GUDID_MODEL = "maude_catalog_eq_gudid_model"
    CATALOG_EQ_CATALOG = "catalog_eq_catalog"
    MAUDE_MODEL_MISSING_OR_PLACEHOLDER = "maude_model_missing_or_placeholder"
    GUDID_MODEL_MISSING = "gudid_model_missing"
    PREFIX_RELATED = "prefix_related"
    CONTAINS = "contains"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class DeviceSide:
    """One side of a pair, MAUDE or GUDID. GUDID product codes are repeated, MAUDE passes a 1-tuple."""
    manufacturer_norm: str | None
    device_name: str | None
    model_raw: str | None
    catalog_raw: str | None
    product_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchContext:
    """What a pair needs beyond its two sides. The alias table comes from the TRAIN split only (E5)."""
    alias_pairs: frozenset[tuple[str, str]]      # (maude labeler, gudid labeler)
    n_dis_for_model: int | None                  # GUDID DIs for (labeler, model key)
    sibling_keys: frozenset[str] = frozenset()   # same-labeler GUDID keys, for E3


# --- Python rules (section 4 of the definition) ----------------------------------------------------
# re.ASCII everywhere: BigQuery RE2 \s is ASCII-only, Python's is Unicode-wide. Without it a
# non-breaking space would be stripped in Python and kept in SQL.
_STRIP = re.compile(STRIP_CHARS_RE, re.ASCII)
_PLACEHOLDER = re.compile(PLACEHOLDER_RE, re.ASCII)
_KIT = re.compile(KIT_WORD_RE, re.ASCII)
_WORD = re.compile(r"[A-Z0-9]+", re.ASCII)


def identifier_key(value: str | None) -> str | None:
    """UPPER(value) with spaces and - _ . / removed. None stays None."""
    return None if value is None else _STRIP.sub("", value.strip()).upper()


def is_placeholder(key: str | None) -> bool:
    """True when the key is missing: under MIN_KEY_LEN characters or matches PLACEHOLDER_RE (E6)."""
    return key is None or len(key) < MIN_KEY_LEN or _PLACEHOLDER.search(key) is not None


def usable_key(value: str | None) -> str | None:
    """identifier_key(value), or None if it is a placeholder."""
    key = identifier_key(value)
    return None if is_placeholder(key) else key


def raw_equal(a: str | None, b: str | None) -> bool:
    """Raw trimmed strings are equal. Guards the E2 hyphen collisions (142-200 vs 14-2200)."""
    return a is not None and b is not None and a.strip() != "" and a.strip() == b.strip()


def contains_as_token(raw: str | None, key: str | None) -> bool:
    """The key appears as a whole token inside the raw string (E2).

    shortcut: a token is one whitespace-delimited word, so a key split across words ("IP 200") is not
    found. Upgrade only if the pilot labels show it matters; SQL can express the same rule with SPLIT.
    """
    return bool(raw) and bool(key) and any(identifier_key(t) == key for t in raw.split())


def _keys(side: DeviceSide) -> tuple[str | None, str | None]:
    return identifier_key(side.model_raw), identifier_key(side.catalog_raw)


def identifier_match(maude: DeviceSide, gudid: DeviceSide) -> MatchKind:
    """Compare every MAUDE identifier with every GUDID identifier, model and catalog (E7).

    Line for line the CASE of query 08. The GUDID side only counts as missing when empty (not when a
    placeholder), exactly as 08 does, so Python reproduces BigQuery.
    """
    m_model, m_cat = _keys(maude)
    g_model, g_cat = _keys(gudid)
    m_missing = is_placeholder(m_model)
    m_cat_ok = not is_placeholder(m_cat)
    if not m_missing and m_model == g_model:
        return MatchKind.SAME_MODEL_KEY
    if not m_missing and m_model == g_cat:
        return MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG
    if m_cat_ok and m_cat == g_model:
        return MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL
    if m_cat_ok and m_cat == g_cat:
        return MatchKind.CATALOG_EQ_CATALOG
    if m_missing:
        return MatchKind.MAUDE_MODEL_MISSING_OR_PLACEHOLDER
    if not g_model:
        return MatchKind.GUDID_MODEL_MISSING
    if g_model.startswith(m_model) or m_model.startswith(g_model):
        return MatchKind.PREFIX_RELATED
    if len(m_model) >= 3 and len(g_model) >= 3 and (m_model in g_model or g_model in m_model):
        return MatchKind.CONTAINS
    return MatchKind.CONFLICT


def is_distinctive(n_dis_for_model: int | None) -> bool:
    """At most NON_DISTINCTIVE_MAX_DIS DIs for this labeler's model key (E4). Unknown count: not distinctive."""
    return n_dis_for_model is not None and n_dis_for_model <= NON_DISTINCTIVE_MAX_DIS


def has_conflicting_identifier(maude: DeviceSide, gudid: DeviceSide,
                               sibling_keys: frozenset[str]) -> bool:
    """GUDID has a record equal to the MAUDE key, and this candidate is only a 1-3 character suffix away (E3a)."""
    g_keys = {k for k in map(usable_key, (gudid.model_raw, gudid.catalog_raw)) if k}
    for mk in filter(None, map(usable_key, (maude.model_raw, maude.catalog_raw))):
        if mk not in sibling_keys:
            continue
        for gk in g_keys:
            short, long_ = sorted((mk, gk), key=len)
            if gk != mk and long_.startswith(short) and len(long_) - len(short) in SUFFIX_LENS:
                return True
    return False


def _words(name: str | None) -> set[str]:
    return set(_WORD.findall((name or "").upper()))


def _is_kit(name: str | None) -> bool:
    return _KIT.search((name or "").upper()) is not None


def pair_features(maude: DeviceSide, gudid: DeviceSide, ctx: MatchContext) -> dict[str, Any]:
    """All feat_* values for one pair. Keys are exactly FEATURE_COLUMNS."""
    kind = identifier_match(maude, gudid)
    m_model, m_cat = _keys(maude)
    g_model, _ = _keys(gudid)
    m_raws, g_raws = (maude.model_raw, maude.catalog_raw), (gudid.model_raw, gudid.catalog_raw)
    m_usable = [k for k in map(usable_key, m_raws) if k]
    g_usable = [k for k in map(usable_key, g_raws) if k]
    m_mfr, g_mfr = maude.manufacturer_norm or None, gudid.manufacturer_norm or None
    wm, wg = _words(maude.device_name), _words(gudid.device_name)
    both_codes = maude.product_codes and gudid.product_codes
    return {
        "feat_match_kind": kind.value,
        "feat_identifier_exact": kind in (MatchKind.SAME_MODEL_KEY, MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG,
                                          MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL, MatchKind.CATALOG_EQ_CATALOG),
        "feat_identifier_cross_field": kind in (MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG,
                                                MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL),
        "feat_raw_identifier_equal": any(raw_equal(a, b) and usable_key(a) for a in m_raws for b in g_raws),
        "feat_token_contains": (any(contains_as_token(r, k) for r in g_raws for k in m_usable)
                                or any(contains_as_token(r, k) for r in m_raws for k in g_usable)),
        "feat_prefix_related": kind == MatchKind.PREFIX_RELATED,
        "feat_conflicting_identifier": has_conflicting_identifier(maude, gudid, ctx.sibling_keys),
        "feat_distinctive": is_distinctive(ctx.n_dis_for_model),
        "feat_n_dis_for_model": ctx.n_dis_for_model,
        "feat_labeler_equal": m_mfr is not None and m_mfr == g_mfr,
        "feat_labeler_alias_equal": (m_mfr, g_mfr) in ctx.alias_pairs,
        "feat_labeler_missing": m_mfr is None or g_mfr is None,
        # None (unknown) when either side has no product code: a mismatch alone proves nothing (E8)
        "feat_product_code_agree": bool(set(maude.product_codes) & set(gudid.product_codes)) if both_codes else None,
        "feat_maude_model_missing": is_placeholder(m_model),
        "feat_maude_catalog_missing": is_placeholder(m_cat),
        "feat_gudid_model_missing": not g_model,
        "feat_device_name_token_overlap": len(wm & wg) / len(wm | wg) if wm and wg else None,
        "feat_maude_name_has_kit_word": _is_kit(maude.device_name),
        "feat_gudid_name_has_kit_word": _is_kit(gudid.device_name),
    }


def feature_vector(features: Mapping[str, Any]) -> tuple:
    """The values in FEATURE_COLUMNS order. Raises on a missing or unknown key."""
    extra, missing = set(features) - set(FEATURE_COLUMNS), set(FEATURE_COLUMNS) - set(features)
    if extra or missing:
        raise KeyError(f"feature keys differ from FEATURE_COLUMNS: missing={sorted(missing)} unknown={sorted(extra)}")
    return tuple(features[c] for c in FEATURE_COLUMNS)


# --- Leakage guards ----------------------------------------------------------------------------------
def assert_no_leakage(columns: Iterable[str]) -> None:
    """Raise LeakageError if is_banned_feature_name() is true for any column."""
    bad = [c for c in columns if is_banned_feature_name(c)]
    if bad:
        raise LeakageError(f"banned feature column names: {bad}")


def assert_no_value_leakage(row: Mapping[str, Any], di_value: str | None) -> None:
    """Raise LeakageError if any feat_* value equals or contains the row's DI, or is a long piece of it."""
    di = (di_value or "").strip()
    if not di:
        return
    for col, val in row.items():
        if not col.startswith("feat_") or val is None or isinstance(val, bool):
            continue
        s = str(val)
        if di in s or (len(s) >= LEAK_MIN_SUBSTRING and s in di):
            raise LeakageError(f"{col}={s!r} carries the DI")


# --- Device identity, split, pair id -----------------------------------------------------------------
def device_key(gudid: DeviceSide) -> str:
    """(GUDID labeler, model stem). Uses GUDID's own labeler name, so no alias table is needed to split.

    shortcut: returns labeler|model key (catalog key if the model is unusable) WITHOUT the stem merge
    (GP200S -> GP200). The merge needs every key of the labeler, so it lives in 00_device_split.sql
    (N2). Downstream code must use that stored key; split_of only hashes the key it is given.
    """
    key = usable_key(gudid.model_raw) or usable_key(gudid.catalog_raw)
    if key is None:
        raise ValueError("GUDID record has no usable model or catalog number, so it has no device key")
    return f"{gudid.manufacturer_norm or ''}|{key}"


def split_bucket(key: str, salt: str = SPLIT_SALT) -> int:
    """int(sha256(salt|key).hexdigest()[:8], 16) % 100. SHA-256 so BigQuery and Python agree."""
    return int(hashlib.sha256(f"{salt}|{key}".encode()).hexdigest()[:8], 16) % 100


def split_of(key: str, test_pct: int = TEST_SPLIT_PCT, salt: str = SPLIT_SALT) -> str:
    """'test' when split_bucket(key) < test_pct, else 'train'."""
    return "test" if split_bucket(key, salt) < test_pct else "train"


def pair_id(mdr_report_key: str, device_seq: int | None, gudid_udi_di: str, role: str) -> str:
    """sha256 of 'mdr|device_seq|di|role', first 32 hex characters. A None device_seq becomes '' (E11)."""
    seq = "" if device_seq is None else str(device_seq)
    return hashlib.sha256(f"{mdr_report_key}|{seq}|{gudid_udi_di}|{role}".encode()).hexdigest()[:32]


# --- SQL emitters: the same constants, returned as BigQuery expressions --------------------------------
def sql_identifier_key(col: str) -> str:
    return f"UPPER(REGEXP_REPLACE(TRIM({col}), r'{STRIP_CHARS_RE}', ''))"


def sql_is_placeholder(key_expr: str) -> str:
    return (f"({key_expr} IS NULL OR LENGTH({key_expr}) < {MIN_KEY_LEN} "
            f"OR REGEXP_CONTAINS({key_expr}, r'{PLACEHOLDER_RE}'))")


def sql_usable_key(col: str) -> str:
    key = sql_identifier_key(col)
    return f"IF({sql_is_placeholder(key)}, NULL, {key})"


def sql_raw_equal(a: str, b: str) -> str:
    return f"(NULLIF(TRIM({a}), '') IS NOT NULL AND TRIM({a}) = TRIM({b}))"


def sql_match_kind(m: str, g: str) -> str:
    """The CASE of query 08, generated from MatchKind. `m` and `g` are column prefixes: it reads the
    already-keyed columns {m}_model, {m}_cat, {g}_model, {g}_cat (outputs of sql_identifier_key)."""
    mm, mc, gm, gc = f"{m}_model", f"{m}_cat", f"{g}_model", f"{g}_cat"
    m_missing = sql_is_placeholder(mm)
    m_cat_ok = f"({mc} IS NOT NULL AND NOT {sql_is_placeholder(mc)})"
    k = MatchKind
    return f"""CASE
    WHEN NOT {m_missing} AND {mm} = {gm} THEN '{k.SAME_MODEL_KEY.value}'
    WHEN NOT {m_missing} AND {mm} = {gc} THEN '{k.MAUDE_MODEL_EQ_GUDID_CATALOG.value}'
    WHEN {m_cat_ok} AND {mc} = {gm} THEN '{k.MAUDE_CATALOG_EQ_GUDID_MODEL.value}'
    WHEN {m_cat_ok} AND {mc} = {gc} THEN '{k.CATALOG_EQ_CATALOG.value}'
    WHEN {m_missing} THEN '{k.MAUDE_MODEL_MISSING_OR_PLACEHOLDER.value}'
    WHEN {gm} IS NULL OR {gm} = '' THEN '{k.GUDID_MODEL_MISSING.value}'
    WHEN STARTS_WITH({gm}, {mm}) OR STARTS_WITH({mm}, {gm}) THEN '{k.PREFIX_RELATED.value}'
    WHEN LENGTH({mm}) >= 3 AND LENGTH({gm}) >= 3
         AND (STRPOS({mm}, {gm}) > 0 OR STRPOS({gm}, {mm}) > 0) THEN '{k.CONTAINS.value}'
    ELSE '{k.CONFLICT.value}'
  END"""


def sql_is_distinctive(n_expr: str) -> str:
    return f"({n_expr} IS NOT NULL AND {n_expr} <= {NON_DISTINCTIVE_MAX_DIS})"


def sql_suffix_lens() -> str:
    """The array used by `FROM ..., UNNEST({{ suffix_lens() }}) AS k` joins (E3)."""
    return f"[{', '.join(map(str, SUFFIX_LENS))}]"


def sql_device_key(mfr: str, model: str, catalog: str) -> str:
    """Python device_key, but NULL instead of raising when neither identifier is usable."""
    key = f"COALESCE({sql_usable_key(model)}, {sql_usable_key(catalog)})"
    return f"IF({key} IS NULL, NULL, CONCAT(IFNULL({mfr}, ''), '|', {key}))"


def sql_split(key_expr: str, test_pct: int = TEST_SPLIT_PCT, salt: str = SPLIT_SALT) -> str:
    bucket = (f"MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(SHA256(CONCAT('{salt}', '|', {key_expr}))), 1, 8)) "
              f"AS INT64), 100)")
    return f"IF({bucket} < {test_pct}, 'test', 'train')"


def sql_pair_id(mdr: str, seq: str, di: str, role: str) -> str:
    return (f"SUBSTR(TO_HEX(SHA256(CONCAT({mdr}, '|', IFNULL(CAST({seq} AS STRING), ''), '|', {di}, '|', "
            f"{role}))), 1, 32)")


# Macro name -> emitter. bq_utils.render() will expand {{ name(args) }} through this table.
SQL_MACROS = {
    "identifier_key": sql_identifier_key,
    "is_placeholder": sql_is_placeholder,
    "usable_key": sql_usable_key,
    "raw_equal": sql_raw_equal,
    "match_kind": sql_match_kind,
    "is_distinctive": sql_is_distinctive,
    "suffix_lens": sql_suffix_lens,
    "device_key": sql_device_key,
    "split": sql_split,
    "pair_id": sql_pair_id,
}
