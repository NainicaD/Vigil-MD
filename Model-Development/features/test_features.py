from features import normalize_key


def test_formatting_differences_match():  # E2
    assert normalize_key("IP-200") == normalize_key("IP200") == "IP200"
    assert normalize_key("5925-040-12") == "592504012"
    assert normalize_key(" cl2078-blue ") == "CL2078BLUE"


def test_placeholders_are_missing():  # E6
    for value in ["NA", "N/A", "n/a", "NI", "UNK", "UNKNOWN"]:
        assert normalize_key(value) is None


def test_empty_short_or_nan_is_missing():
    assert normalize_key("") is None
    assert normalize_key("7") is None
    assert normalize_key(None) is None
    assert normalize_key(float("nan")) is None


def test_hyphen_collision_is_a_known_risk():  # E2 exception
    # different devices that collapse to one key; the raw-equality flag (step 3) guards this
    assert normalize_key("142-200") == normalize_key("14-2200")