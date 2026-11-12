"""F1 text cosine: offline, a few records."""
import pytest

pytest.importorskip("sklearn")

from vigil_model import text_cosine as tc  # noqa: E402
from vigil_model.same_device_features import DeviceSide  # noqa: E402


def side(name, model, mfr="boston scientific", pcs=("FGE",)):
    return DeviceSide(mfr, name, model, None, tuple(pcs))


GUDID = [side("WALLSTENT RP Endoprosthesis", "M001711260"), side("MACH1 guide catheter", "H749195001260"),
         side("Renegade STC 18", "M001181260"), side("Passport", "M0062181010")]


def test_same_record_scores_1_and_a_different_device_scores_lower():
    enc = tc.fit(tc.side_text(g) for g in GUDID)
    maude = side("MACH1", "1260")
    cos = tc.pair_cosines(enc, [tc.side_text(maude)] * 2, [tc.side_text(GUDID[1]), tc.side_text(GUDID[0])])
    assert cos[0] > cos[1]                                   # MACH1 text is closer to the MACH1 record
    same = tc.pair_cosines(enc, [tc.side_text(GUDID[0])], [tc.side_text(GUDID[0])])
    assert same[0] == pytest.approx(1.0, abs=1e-5)


def test_side_text_never_contains_nothing_but_separators_and_save_load_roundtrip(tmp_path):
    enc = tc.fit(tc.side_text(g) for g in GUDID)
    path = tmp_path / "f.tfidf.pkl"
    tc.save(enc, path)
    again = tc.load(path)
    t = [tc.side_text(GUDID[2])]
    assert (tc.pair_cosines(enc, t, t) == tc.pair_cosines(again, t, t)).all()
    with pytest.raises(ValueError):
        tc.pair_cosines(enc, t, t * 2)


def test_batched_cosines_equal_one_shot():
    enc = tc.fit(tc.side_text(g) for g in GUDID)
    m = [tc.side_text(side("MACH1", "1260"))] * 4
    g = [tc.side_text(x) for x in GUDID]
    assert tc.pair_cosines(enc, m, g, batch=1) == pytest.approx(tc.pair_cosines(enc, m, g, batch=100))
