"""Sentence embeddings as an sklearn step, for the MiniLM router candidates (item C).

    SentenceEmbedder()        all-MiniLM-L6-v2: 384 numbers per question, length 1

The encoder is a frozen, pretrained model: fit() learns nothing, transform() encodes. Only the
logistic regression after it is trained, so a candidate is chosen and its thresholds tuned on
validation exactly like the TF-IDF models (train.fit_and_score).

torch and sentence-transformers are imported only when a question is encoded, so the rest of
the router (and CI, which does not install them) never needs them. Install them from
requirements-tf.txt.

Saving: the pickled step (model.joblib) holds the settings only, never the weights. save_encoder()
writes the weights next to it (models/<dir>/encoder/), and predict.QueryRouter.load points the
step at that folder, so a saved candidate does not download anything at run time.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
ENCODER_DIR = "encoder"

# Embeddings per (encoder, question), shared by every grid setting of one run: the encoder is
# frozen, so a question's embedding never changes. Not saved with the model.
_CACHE: dict[tuple[str, str], np.ndarray] = {}
_MODELS: dict[str, object] = {}


def _device() -> str:
    import torch  # pylint: disable=import-outside-toplevel
    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _encoder(name: str):
    if name not in _MODELS:
        from sentence_transformers import SentenceTransformer  # pylint: disable=import-outside-toplevel
        _MODELS[name] = SentenceTransformer(name, device=_device())
    return _MODELS[name]


class SentenceEmbedder(BaseEstimator, TransformerMixin):
    """Questions -> normalised sentence embeddings (n, dim)."""

    def __init__(self, model_name: str = DEFAULT_MODEL, batch_size: int = 128):
        self.model_name = model_name
        self.batch_size = batch_size

    def fit(self, X, y=None):  # pylint: disable=unused-argument
        return self

    def transform(self, X) -> np.ndarray:
        texts = [str(x) for x in X]
        todo = list(dict.fromkeys(t for t in texts if (self.model_name, t) not in _CACHE))
        if todo:
            vecs = _encoder(self.model_name).encode(
                todo, batch_size=self.batch_size, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False)
            for t, v in zip(todo, vecs):
                _CACHE[(self.model_name, t)] = v.astype(np.float32)
        return np.vstack([_CACHE[(self.model_name, t)] for t in texts])

    def save_encoder(self, model_dir: Path) -> Path:
        """Writes the encoder's weights to model_dir/encoder/ and returns that folder."""
        out = Path(model_dir) / ENCODER_DIR
        _encoder(self.model_name).save(str(out))
        return out

    def use_saved_encoder(self, model_dir: Path) -> None:
        """Points the step at the weights saved by save_encoder()."""
        folder = Path(model_dir) / ENCODER_DIR
        if not folder.is_dir():
            raise FileNotFoundError(f"no saved encoder in {folder}")
        self.model_name = str(folder)
