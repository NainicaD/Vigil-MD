"""A fine-tuned DistilBERT as an sklearn classifier, for the router candidate "distilbert" (item C).

    FineTunedClassifier()     distilbert-base-uncased, one sigmoid output per label (multi-label)

Unlike the MiniLM candidates (embed.py, a frozen encoder before a logistic regression), every
weight of the network is trained on the router's questions. fit(X, Y) trains, predict_proba(X)
returns one probability per label, so train.fit_and_score tunes its thresholds on validation and
applies the rule backstop exactly as for the TF-IDF models, and the gate scores it the same way.

torch and transformers are imported only in fit() / predict_proba(), so the rest of the router
(and CI, which does not install them) never needs them. Install them from requirements-tf.txt.

Saving: the pickled step (model.joblib) holds the settings only, never the weights.
save_weights() writes the fine-tuned network and its tokenizer to models/<dir>/finetuned/, and
predict.QueryRouter.load points the step at that folder.
"""
from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin

DEFAULT_MODEL = "distilbert-base-uncased"
WEIGHTS_DIR = "finetuned"


def _device() -> str:
    import torch  # pylint: disable=import-outside-toplevel
    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


class FineTunedClassifier(BaseEstimator, ClassifierMixin):
    """Questions -> per-label probabilities from a fine-tuned transformer."""

    def __init__(self, model_name: str = DEFAULT_MODEL, lr: float = 5e-5, epochs: int = 3,
                 batch_size: int = 32, max_len: int = 64, weight_decay: float = 0.01,
                 warmup: float = 0.1, seed: int = 0):
        self.model_name, self.lr, self.epochs, self.batch_size = model_name, lr, epochs, batch_size
        self.max_len, self.weight_decay, self.warmup, self.seed = max_len, weight_decay, warmup, seed

    # ── training ─────────────────────────────────────────────────────────────
    def fit(self, X, Y):
        import torch  # pylint: disable=import-outside-toplevel
        from transformers import (AutoModelForSequenceClassification,  # pylint: disable=import-outside-toplevel
                                  AutoTokenizer, get_linear_schedule_with_warmup)
        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        texts, Y = [str(x) for x in X], np.asarray(Y, dtype=np.float32)
        self.n_labels_ = Y.shape[1]
        self.tokenizer_ = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name, num_labels=self.n_labels_, problem_type="multi_label_classification")
        device = _device()
        model.to(device).train()
        opt = torch.optim.AdamW(model.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        steps = self.epochs * int(np.ceil(len(texts) / self.batch_size))
        sched = get_linear_schedule_with_warmup(opt, int(self.warmup * steps), steps)
        rng = np.random.default_rng(self.seed)
        for _ in range(self.epochs):
            order = rng.permutation(len(texts))
            for start in range(0, len(texts), self.batch_size):
                idx = order[start:start + self.batch_size]
                batch = self._encode([texts[i] for i in idx], device)
                out = model(**batch, labels=torch.tensor(Y[idx], device=device))
                out.loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                sched.step()
                opt.zero_grad()
        self.model_ = model.eval()
        self.classes_ = np.arange(self.n_labels_)
        return self

    def _encode(self, texts: list[str], device: str):
        enc = self.tokenizer_(texts, padding=True, truncation=True, max_length=self.max_len,
                              return_tensors="pt")
        return {k: v.to(device) for k, v in enc.items()}

    # ── prediction ───────────────────────────────────────────────────────────
    def predict_proba(self, X) -> np.ndarray:
        import torch  # pylint: disable=import-outside-toplevel
        texts, device, out = [str(x) for x in X], _device(), []
        self.model_.to(device)
        with torch.no_grad():
            for start in range(0, len(texts), 128):
                logits = self.model_(**self._encode(texts[start:start + 128], device)).logits
                out.append(torch.sigmoid(logits).float().cpu().numpy())
        return np.vstack(out) if out else np.zeros((0, self.n_labels_), dtype=np.float32)

    def predict(self, X) -> np.ndarray:
        return (self.predict_proba(X) >= 0.5).astype(int)

    # ── saving ───────────────────────────────────────────────────────────────
    def __getstate__(self):
        """model.joblib keeps the settings only; the weights are saved by save_weights()."""
        return {k: v for k, v in self.__dict__.items() if k not in ("model_", "tokenizer_")}

    def save_weights(self, model_dir: Path) -> Path:
        out = Path(model_dir) / WEIGHTS_DIR
        self.model_.save_pretrained(str(out))
        self.tokenizer_.save_pretrained(str(out))
        return out

    def use_saved_weights(self, model_dir: Path) -> None:
        """Loads the network and tokenizer written by save_weights() (no download)."""
        from transformers import (AutoModelForSequenceClassification,  # pylint: disable=import-outside-toplevel
                                  AutoTokenizer)
        folder = Path(model_dir) / WEIGHTS_DIR
        if not folder.is_dir():
            raise FileNotFoundError(f"no saved fine-tuned weights in {folder}")
        self.tokenizer_ = AutoTokenizer.from_pretrained(str(folder))
        self.model_ = AutoModelForSequenceClassification.from_pretrained(str(folder)).eval()
