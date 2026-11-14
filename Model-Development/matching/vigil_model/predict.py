"""Score candidate pairs with a final model bundle (written by scripts/s11_package_final.py).

A bundle is a folder with model.json (XGBoost), tfidf.pkl (the F1 encoder s04 fitted on the training
GUDID texts), and meta.json (cutoff, feature columns, lineage). The IE gate runs before the cutoff,
so a pair without a usable MAUDE identifier, only prefix-related, or (gate e6_e3b_c1) a mid-string
contains hit with disagreeing product codes, is never a MATCH. meta.json "ie_gate" names the version.

    from vigil_model.predict import load
    bundle = load("models/final_v1")
    feats = bundle.features(maude_side, gudid_side, ctx)      # one dict of feat_* values
    bundle.decide(pd.DataFrame([feats]))                     # score + decision per row
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from vigil_model import text_cosine
from vigil_model.same_device_features import (
    DeviceSide, MatchContext, MatchKind, insufficient_evidence, pair_features,
)

MATCH, NON_MATCH, IE = "MATCH", "NON_MATCH", "INSUFFICIENT_EVIDENCE"


def to_float(series: pd.Series) -> pd.Series:
    """Same as s06_train_models.to_float: True/False/number/empty -> 1.0/0.0/number/NaN."""
    text = series.astype(str).str.strip().str.upper()
    return text.map({"TRUE": 1.0, "FALSE": 0.0}).fillna(pd.to_numeric(series, errors="coerce"))


def encode(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Same as s06_train_models.encode, for the given columns."""
    X = pd.DataFrame(index=df.index)
    for col in cols:
        if col == "feat_match_kind":
            X[col] = pd.Categorical(df[col], categories=[k.value for k in MatchKind])
        else:
            X[col] = to_float(df[col])
    return X


@dataclasses.dataclass
class Bundle:
    model: XGBClassifier
    encoder: text_cosine.TfidfEncoder
    meta: dict

    @property
    def cutoff(self) -> float:
        return float(self.meta["cutoff"])

    def features(self, maude: DeviceSide, gudid: DeviceSide, ctx: MatchContext) -> dict:
        """All feat_* values of one pair, with the text cosine from the bundle's own encoder."""
        cosine = float(text_cosine.pair_cosines(self.encoder, [text_cosine.side_text(maude)],
                                                [text_cosine.side_text(gudid)])[0])
        return pair_features(maude, gudid, dataclasses.replace(ctx, text_cosine=round(cosine, 6)))

    def decide(self, feats: pd.DataFrame) -> pd.DataFrame:
        """score (model probability) and decision per row. The gate needs feat_match_kind and the two
        missing-identifier flags even when the model itself does not use them."""
        score = self.model.predict_proba(encode(feats, self.meta["feature_columns"]))[:, 1]
        # the gate version the bundle was evaluated with; v1.0.0 and older name e6_e3b
        version = self.meta.get("ie_gate", "e6_e3b")
        gated = feats.apply(insufficient_evidence, axis=1, version=version).to_numpy(dtype=bool)
        decision = np.where(gated, IE, np.where(score >= self.cutoff, MATCH, NON_MATCH))
        return pd.DataFrame({"score": score, "decision": decision}, index=feats.index)


def load(bundle_dir: str | Path) -> Bundle:
    """Only load bundles from the team's registry: tfidf.pkl is a pickle and runs code when loaded."""
    d = Path(bundle_dir)
    meta = json.loads((d / "meta.json").read_text())
    model = XGBClassifier()
    model.load_model(d / "model.json")
    return Bundle(model, text_cosine.load(d / "tfidf.pkl"), meta)
