"""DistilBERT router candidate (item C): query_router/finetune.py and its use in train, compare and
predict. The network is replaced by a word-count fake, so these tests need neither torch nor a download."""
import json
import pickle

import numpy as np
import pytest

from query_router import compare
from query_router import finetune
from query_router import generate as gen
from query_router.labels import LABELS
from query_router.predict import QueryRouter
from query_router.train import FINETUNED_TYPES, build_pipeline, fit_and_score


@pytest.fixture
def fake(monkeypatch):
    """fit learns, per label, the words of its questions; predict_proba scores word overlap."""
    calls = {"saved": [], "loaded": []}

    def fit(self, X, Y):
        Y = np.asarray(Y)
        self.n_labels_ = Y.shape[1]
        self.model_ = [set(w for x, y in zip(X, Y) if y[j] for w in str(x).lower().split())
                       for j in range(self.n_labels_)]
        self.tokenizer_ = "tok"
        self.classes_ = np.arange(self.n_labels_)
        return self

    def predict_proba(self, X):
        out = np.zeros((len(X), self.n_labels_))
        for i, x in enumerate(X):
            words = set(str(x).lower().split())
            for j, vocab in enumerate(self.model_):
                out[i, j] = len(words & vocab) / max(len(words), 1)
        return out

    def save_weights(self, model_dir):
        (model_dir / finetune.WEIGHTS_DIR).mkdir(parents=True, exist_ok=True)
        (model_dir / finetune.WEIGHTS_DIR / "vocab.json").write_text(json.dumps([sorted(v) for v in self.model_]))
        calls["saved"].append(model_dir)

    def use_saved_weights(self, model_dir):
        self.model_ = [set(v) for v in json.loads((model_dir / finetune.WEIGHTS_DIR / "vocab.json").read_text())]
        calls["loaded"].append(model_dir)

    for name, fn in (("fit", fit), ("predict_proba", predict_proba), ("save_weights", save_weights),
                     ("use_saved_weights", use_saved_weights)):
        monkeypatch.setattr(finetune.FineTunedClassifier, name, fn)
    return calls


@pytest.fixture(scope="module")
def data():
    return gen.generate(n_train=400, n_val=150, seed=7)


def test_build_pipeline_makes_a_finetuned_step_with_its_settings():
    pipe = build_pipeline(model="distilbert", lr=3e-5, epochs=5, seed=2)
    clf = pipe.named_steps["clf"]
    assert isinstance(clf, finetune.FineTunedClassifier) and list(pipe.named_steps) == ["clf"]
    assert (clf.lr, clf.epochs, clf.seed, clf.model_name) == (3e-5, 5, 2, "distilbert-base-uncased")
    assert "distilbert" in FINETUNED_TYPES


def test_the_pickle_holds_settings_not_weights():
    clf = finetune.FineTunedClassifier(lr=1e-5)
    clf.model_, clf.tokenizer_, clf.n_labels_ = object(), object(), 6
    state = pickle.loads(pickle.dumps(clf)).__dict__
    assert "model_" not in state and "tokenizer_" not in state and state["lr"] == 1e-5 and state["n_labels_"] == 6


def test_it_goes_through_the_same_threshold_tuning_and_rules(fake, data):
    pipe, metrics, thresholds = fit_and_score({"model": "distilbert", "lr": 5e-5, "epochs": 3},
                                              data["train"], data["val"])
    assert set(thresholds) == set(LABELS) and 0 <= metrics["macro_f1"] <= 1
    assert metrics["advice_recall"] is not None                    # scored with the rule backstop


def test_compare_grid_uses_settings_not_c(fake, data):
    best = compare.best_per_type(data["train"], data["val"],
                                 grids={"distilbert": compare.GRIDS["distilbert"]})
    params = best["distilbert"][0]
    assert params["model"] == "distilbert" and "C" not in params and {"lr", "epochs"} <= set(params)


def test_a_saved_candidate_loads_its_weights_and_classifies(fake, data, tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "MODELS_DIR", tmp_path)
    pipe, metrics, thresholds = fit_and_score({"model": "distilbert", "lr": 5e-5, "epochs": 3},
                                              data["train"], data["val"])
    meta = compare.save_candidate("distilbert", {"model": "distilbert", "lr": 5e-5, "epochs": 3},
                                  pipe, metrics, thresholds, 400, 150)
    out = tmp_path / "distilbert"
    assert meta["finetuned"] == "distilbert-base-uncased" and meta["model"] == "distilbert"
    assert (out / "model.joblib").exists() and (out / finetune.WEIGHTS_DIR / "vocab.json").exists()
    router = QueryRouter.load(out)
    assert fake["loaded"] == [out]
    r = router.classify("How many deaths has the insulin pump had?")
    assert set(r["scores"]) == set(LABELS) and r["unknown_word_share"] is None     # no TF-IDF vocabulary
