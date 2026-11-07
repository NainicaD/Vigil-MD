from features import normalize_key, identifier_match


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


def test_formatting_only_is_exact():  # E2
    assert identifier_match(["IP-200", None], ["IP200", None]) == (1, 0)


def test_catalog_matches_model_across_fields():  # E7
    # MAUDE model is a placeholder, MAUDE catalog = GUDID model
    assert identifier_match(["NA", "1006402"], ["1006402", None]) == (1, 0)


def test_whole_token_match():  # E2
    assert identifier_match(["CV-190", None], ["OLYMPUS CV-190", None]) == (0, 1)


def test_placeholders_never_match():  # E6
    assert identifier_match(["NA", "UNK"], ["NA", "UNK"]) == (0, 0)


def test_suffix_variant_is_not_a_match():  # E3
    assert identifier_match(["GP200", None], ["GP200S", None]) == (0, 0)


def test_known_gap_gf_uct180():  # E2 example the rule does not cover (flagged to Hitika)
    assert identifier_match(["GF-UCT180", None], ["OLYMPUS GF TYPE UCT180", None]) == (0, 0)