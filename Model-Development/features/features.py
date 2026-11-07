import math
import re

# values that mean "no identifier", checked after normalizing (E6)
PLACEHOLDERS = {"NA", "NI", "UNK", "UNKNOWN"}


def normalize_key(value):
    """Turn a raw model or catalog number into a comparable key, or None if unusable."""
    # missing values: None, or NaN (how pandas stores empty cells)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None

    # uppercase, remove spaces and - _ . /  (E2)
    key = re.sub(r"[\s\-_./]", "", str(value).strip()).upper()

    # too short or a placeholder means missing (E6)
    if len(key) < 2 or key in PLACEHOLDERS:
        return None
    return key


def tokens(value):
    """Split a raw field on spaces and normalize each word (for whole-token matching)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return set()
    keys = {normalize_key(part) for part in str(value).split()}
    keys.discard(None)   # drop placeholders and short bits
    return keys


def identifier_match(maude_ids, gudid_ids):
    """Compare every MAUDE identifier with every GUDID identifier (E2, E7).

    maude_ids and gudid_ids are lists of raw values, like [model, catalog].
    Returns two flags (0 or 1):
      exact      - a normalized key is equal on both sides
      token_only - no exact match, but a key appears as a whole word on the other side
    """
    maude_keys = {normalize_key(v) for v in maude_ids}
    maude_keys.discard(None)
    gudid_keys = {normalize_key(v) for v in gudid_ids}
    gudid_keys.discard(None)

    # exact: any key shared by both sides (& means "in both sets")
    exact = len(maude_keys & gudid_keys) > 0

    # token: a key from one side is a whole word on the other side
    maude_words = set()
    for v in maude_ids:
        maude_words |= tokens(v)
    gudid_words = set()
    for v in gudid_ids:
        gudid_words |= tokens(v)
    token = len(maude_keys & gudid_words) > 0 or len(gudid_keys & maude_words) > 0

    return int(exact), int(token and not exact)


def raw_values(ids):
    """Trimmed, uppercased raw identifiers, skipping missing values and placeholders."""
    values = set()
    for v in ids:
        if normalize_key(v) is not None:
            values.add(str(v).strip().upper())
    return values


def raw_identifier_equal(maude_ids, gudid_ids):
    """1 if any identifier is equal BEFORE removing - _ . /  (guards E2 collisions)."""
    return int(len(raw_values(maude_ids) & raw_values(gudid_ids)) > 0)


def clean_name(value):
    """Lowercase and trim a manufacturer name, or None if missing."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    name = str(value).strip().lower()
    if name == "":
        return None
    return name


def build_alias_table(linked_rows, min_dis=3):
    """Learn manufacturer aliases from UDI-linked rows of the TRAIN split only (E5).

    linked_rows: list of (maude_name, gudid_name, udi_di).
    Keeps a name pair only if it shows up with at least min_dis different devices,
    so one-off typos stay out (open question 3).
    Returns a set of (maude_name, gudid_name) pairs.
    """
    devices_per_pair = {}
    for maude_name, gudid_name, udi_di in linked_rows:
        m = clean_name(maude_name)
        g = clean_name(gudid_name)
        if m is None or g is None or m == g:
            continue   # missing, or already equal: not an alias
        if (m, g) not in devices_per_pair:
            devices_per_pair[(m, g)] = set()
        devices_per_pair[(m, g)].add(udi_di)

    aliases = set()
    for pair, devices in devices_per_pair.items():
        if len(devices) >= min_dis:
            aliases.add(pair)
    return aliases


def manufacturer_match(maude_name, gudid_name, aliases):
    """Returns two flags (0 or 1): name_equal, alias_match (E5)."""
    m = clean_name(maude_name)
    g = clean_name(gudid_name)
    if m is None or g is None:
        return 0, 0
    if m == g:
        return 1, 0
    return 0, int((m, g) in aliases)


def build_di_counts(gudid_rows):
    """Count different DIs per (labeler, model key) across all of GUDID (E4).

    gudid_rows: list of (labeler_name, model_number, udi_di).
    Returns a dict {(labeler, model_key): number of different DIs}.
    """
    devices_per_key = {}
    for labeler, model_number, udi_di in gudid_rows:
        lab = clean_name(labeler)
        key = normalize_key(model_number)
        if lab is None or key is None:
            continue
        if (lab, key) not in devices_per_key:
            devices_per_key[(lab, key)] = set()
        devices_per_key[(lab, key)].add(udi_di)

    counts = {}
    for pair, devices in devices_per_key.items():
        counts[pair] = len(devices)
    return counts


def model_key_di_count(labeler, model_number, di_counts):
    """How many GUDID DIs share this labeler + model key (0 if unknown or missing)."""
    lab = clean_name(labeler)
    key = normalize_key(model_number)
    if lab is None or key is None:
        return 0
    return di_counts.get((lab, key), 0)


def is_distinctive(labeler, model_number, di_counts, max_dis=20):
    """1 if the model key is shared by at most max_dis DIs of this labeler (E4)."""
    n = model_key_di_count(labeler, model_number, di_counts)
    return int(1 <= n <= max_dis)


def build_labeler_keys(di_counts):
    """Every model key each labeler has in GUDID: {labeler: set of keys}."""
    labeler_keys = {}
    for lab, key in di_counts:
        if lab not in labeler_keys:
            labeler_keys[lab] = set()
        labeler_keys[lab].add(key)
    return labeler_keys


def is_suffix_variant(a, b, max_extra=3):
    """True if one key is the other plus 1 to max_extra characters (GP200 vs GP200S)."""
    if len(a) <= len(b):
        shorter, longer = a, b
    else:
        shorter, longer = b, a
    extra = len(longer) - len(shorter)
    return 1 <= extra <= max_extra and longer.startswith(shorter)


def suffix_conflict(maude_ids, gudid_ids, gudid_labeler, labeler_keys):
    """Two flags (0 or 1) for suffix variants (E3):
      conflicting_sibling - candidate is a suffix variant of the MAUDE key AND the labeler
                            has a record equal to the MAUDE key exactly (E3a: NON_MATCH)
      prefix_only         - suffix variant, but no exact record exists (E3b: INSUFFICIENT_EVIDENCE)
    """
    maude_keys = {normalize_key(v) for v in maude_ids}
    maude_keys.discard(None)
    gudid_keys = {normalize_key(v) for v in gudid_ids}
    gudid_keys.discard(None)
    known_keys = labeler_keys.get(clean_name(gudid_labeler), set())

    conflict = False
    prefix = False
    for mk in maude_keys:
        for gk in gudid_keys:
            if is_suffix_variant(mk, gk):
                if mk in known_keys:
                    conflict = True
                else:
                    prefix = True

    return int(conflict), int(prefix and not conflict)


# fixed column order, used by every track in training and serving
FEATURE_NAMES = [
    "id_exact",
    "id_token_only",
    "raw_identifier_equal",
    "mfr_equal",
    "mfr_alias",
    "id_and_mfr",
    "model_key_di_count",
    "model_key_distinctive",
    "conflicting_sibling",
    "prefix_only",
    "product_code_match",
    "maude_has_identifier",
]


def product_code_match(maude_code, gudid_code):
    """1 if equal, 0 if different, None if either is missing (E8: soft evidence)."""
    m = clean_name(maude_code)
    g = clean_name(gudid_code)
    if m is None or g is None:
        return None
    return int(m == g)


def make_features(pair, aliases, di_counts, labeler_keys):
    """Turn one MAUDE-GUDID pair into the shared feature row. Every track uses this.

    pair: dict with maude_model, maude_catalog, maude_manufacturer, maude_product_code,
          gudid_model, gudid_catalog, gudid_labeler, gudid_product_code
    """
    maude_ids = [pair.get("maude_model"), pair.get("maude_catalog")]
    gudid_ids = [pair.get("gudid_model"), pair.get("gudid_catalog")]
    labeler = pair.get("gudid_labeler")

    id_exact, id_token_only = identifier_match(maude_ids, gudid_ids)
    mfr_equal, mfr_alias = manufacturer_match(pair.get("maude_manufacturer"), labeler, aliases)
    conflict, prefix_only = suffix_conflict(maude_ids, gudid_ids, labeler, labeler_keys)

    # feature cross from the plan: identifier AND manufacturer both agree
    id_and_mfr = int(id_exact == 1 and (mfr_equal == 1 or mfr_alias == 1))

    maude_keys = [normalize_key(v) for v in maude_ids]
    has_identifier = int(any(k is not None for k in maude_keys))

    return {
        "id_exact": id_exact,
        "id_token_only": id_token_only,
        "raw_identifier_equal": raw_identifier_equal(maude_ids, gudid_ids),
        "mfr_equal": mfr_equal,
        "mfr_alias": mfr_alias,
        "id_and_mfr": id_and_mfr,
        "model_key_di_count": model_key_di_count(labeler, pair.get("gudid_model"), di_counts),
        "model_key_distinctive": is_distinctive(labeler, pair.get("gudid_model"), di_counts),
        "conflicting_sibling": conflict,
        "prefix_only": prefix_only,
        "product_code_match": product_code_match(
            pair.get("maude_product_code"), pair.get("gudid_product_code")
        ),
        "maude_has_identifier": has_identifier,
    }