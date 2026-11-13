"""Final-bundle scoring: the IE gate wins over the score, the cutoff decides the rest. Offline."""
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")
pytest.importorskip("sklearn")

from xgboost import XGBClassifier  # noqa: E402

from vigil_model import predict, text_cosine  # noqa: E402
from vigil_model.same_device_features import DeviceSide, MatchContext, MatchKind  # noqa: E402

COLS = ["feat_text_cosine"]


@pytest.fixture
def bundle(tmp_path):
    rng = np.random.default_rng(0)
    cos = rng.uniform(0, 1, 400)
    model = XGBClassifier(n_estimators=20, max_depth=2)
    model.fit(pd.DataFrame({"feat_text_cosine": cos}), (cos > 0.5).astype(int))
    model.save_model(tmp_path / "model.json")
    gudid = [DeviceSide("medtronic", "pacemaker", "A1", None, ("DXY",)),
             DeviceSide("medtronic", "pacemaker lead", "A2", None, ("DXY",)),
             DeviceSide("boston scientific", "WALLSTENT", "M001711260", None, ("FGE",))]
    enc = text_cosine.fit(text_cosine.side_text(g) for g in gudid)   # min_df needs n-grams shared by 2+ texts
    text_cosine.save(enc, tmp_path / "tfidf.pkl")
    (tmp_path / "meta.json").write_text(json.dumps({"cutoff": 0.5, "feature_columns": COLS}))
    return predict.load(tmp_path)


def row(cos, kind=MatchKind.SAME_MODEL_KEY.value, model_missing="False", catalog_missing="False"):
    return {"feat_text_cosine": cos, "feat_match_kind": kind,
            "feat_maude_model_missing": model_missing, "feat_maude_catalog_missing": catalog_missing}


def test_gate_beats_a_high_score_and_cutoff_decides_the_rest(bundle):
    feats = pd.DataFrame([
        row(0.95),                                                    # MATCH
        row(0.05),                                                    # NON_MATCH
        row(0.95, model_missing="True", catalog_missing="True"),      # E6: no identifier -> IE
        row(0.95, kind=MatchKind.PREFIX_RELATED.value),               # E3b -> IE
        row(0.95, model_missing="True"),                              # catalog still usable -> scored
    ])
    out = bundle.decide(feats)
    assert out["decision"].tolist() == [predict.MATCH, predict.NON_MATCH, predict.IE, predict.IE, predict.MATCH]
    assert out["score"].iloc[0] > 0.5 > out["score"].iloc[1]


def test_bundle_applies_the_gate_version_it_names(bundle):
    c1 = dict(row(0.95, kind=MatchKind.CONTAINS.value), feat_contains_at_end="False", feat_product_code_agree="False")
    feats = pd.DataFrame([c1])
    assert bundle.decide(feats)["decision"].tolist() == [predict.MATCH]      # no "ie_gate": v1.0.0's e6_e3b
    bundle.meta["ie_gate"] = "e6_e3b_c1"
    assert bundle.decide(feats)["decision"].tolist() == [predict.IE]


def test_features_use_the_bundle_encoder_for_the_cosine(bundle):
    side = DeviceSide("medtronic", "pacemaker", "A1", None, ("DXY",))
    feats = bundle.features(side, side, MatchContext(alias_pairs=frozenset(), n_dis_for_model=1,
                                                     sibling_keys=frozenset(), text_cosine=None))
    assert feats["feat_text_cosine"] == pytest.approx(1.0, abs=1e-5)
