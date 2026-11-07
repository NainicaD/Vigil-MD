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