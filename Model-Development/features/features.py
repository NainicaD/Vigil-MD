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