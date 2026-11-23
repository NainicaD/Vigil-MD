"""MiniLM router candidates (item C): query_router/embed.py and its use in train, compare and
predict. A fake encoder stands in for MiniLM, so these tests need neither torch nor a download."""
import hashlib
import json
import pickle
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from query_router import compare
from query_router import embed
from query_router import generate as gen
from query_router.predict import QueryRouter, _steps
from query_router.train import build_pipeline

CHATBOT = Path(__file__).resolve().parents[1]


class FakeEncoder:
    """Deterministic 16-number vectors from the words of a question (unit length)."""

    def __init__(self):
        self.encoded = 0

    def encode(self, texts, **_):
        self.encoded += len(texts)
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 16] += 1.0
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def save(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / "config.json").write_text("{}")


@pytest.fixture
def fake(monkeypatch):
    enc = FakeEncoder()
    monkeypatch.setattr(embed, "_CACHE", {})
    monkeypatch.setattr(embed, "_encoder", lambda name: enc)
    return enc


@pytest.fixture(scope="module")
def data():
    return gen.generate(n_train=400, n_val=150, seed=7)


def test_embeddings_are_cached_per_question(fake):
    step = embed.SentenceEmbedder()
    a = step.transform(["any deaths?", "any deaths?", "who makes it"])
    assert a.shape == (3, 16) and fake.encoded == 2          # duplicates encoded once
    step.transform(["any deaths?"])
    assert fake.encoded == 2                                 # and never again


def test_the_pickled_step_holds_settings_not_weights(fake):
    step = embed.SentenceEmbedder()
    step.transform(["any deaths?"])
    assert set(vars(pickle.loads(pickle.dumps(step)))) == {"model_name", "batch_size"}


@pytest.mark.parametrize("kind", ["minilm", "minilm_tfidf"])
def test_embedding_pipelines_fit_and_predict(fake, data, kind):
    rows = data["train"]
    pipe = build_pipeline(C=4.0, model=kind).fit(
        [r["text"] for r in rows], np.array([[int(l in r["labels"]) for l in
                                              ("counts", "problems", "recalls", "narratives",
                                               "identity", "out_of_scope")] for r in rows]))
    assert pipe.predict_proba(["Was the DreamStation recalled?"]).shape == (1, 6)


def test_a_saved_candidate_loads_its_own_encoder(fake, data, tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "MODELS_DIR", tmp_path)
    best = compare.best_per_type(data["train"], data["val"], grids={"minilm_tfidf": (1.0,)})
    params, pipe, scores, thresholds = best["minilm_tfidf"]
    meta = compare.save_candidate("minilm_tfidf", params, pipe, scores, thresholds, 400, 150)
    out = tmp_path / "minilm_tfidf"
    assert meta["model"] == "minilm_tfidf" and meta["encoder"] == embed.DEFAULT_MODEL
    assert json.loads((out / "meta.json").read_text())["encoder"] == embed.DEFAULT_MODEL
    assert (out / embed.ENCODER_DIR / "config.json").exists()
    router = QueryRouter.load(out)
    [step] = [s for s in _steps(router.pipe) if isinstance(s, embed.SentenceEmbedder)]
    assert step.model_name == str(out / embed.ENCODER_DIR)   # local weights, no download
    out_ = router.classify("Was the DreamStation recalled?")
    assert set(out_) >= {"labels", "run_queries", "scores"}
    assert out_["unknown_word_share"] is not None             # TF-IDF part still reports drift


def test_a_candidate_without_its_encoder_folder_is_refused(fake, data, tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "MODELS_DIR", tmp_path)
    best = compare.best_per_type(data["train"], data["val"], grids={"minilm": (1.0,)})
    compare.save_candidate("minilm", *best["minilm"], 400, 150)
    (tmp_path / "minilm" / embed.ENCODER_DIR / "config.json").unlink()
    (tmp_path / "minilm" / embed.ENCODER_DIR).rmdir()
    with pytest.raises(FileNotFoundError, match="no saved encoder"):
        QueryRouter.load(tmp_path / "minilm")


def test_the_default_comparison_does_not_include_minilm():
    assert set(compare.DEFAULT_TYPES) == {"lr", "svm", "nb"}


def test_the_router_and_chatbot_import_without_torch():
    # CI installs requirements.txt only: nothing outside embed.py may import torch.
    code = ("import sys; import query_router.train, query_router.predict, query_router.compare, "
            "query_router.embed, answer.pipeline, api.app; "
            "print('ok' if not {'torch', 'sentence_transformers'} & set(sys.modules) else "
            "'torch imported')")
    done = subprocess.run([sys.executable, "-c", code], cwd=CHATBOT, capture_output=True,
                          text=True, check=False)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-2000:]
