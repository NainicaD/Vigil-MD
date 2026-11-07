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