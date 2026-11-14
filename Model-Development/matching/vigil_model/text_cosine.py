"""F1: TF-IDF character n-gram cosine between the MAUDE and the GUDID record text of a pair.

Same text template and encoder as the embedding retrieval prototype (embedding_retrieval.record_text,
TfidfEncoder), which found Recall@10 97.1%, and as diag D11 (AUC 0.998 where the identifiers agree).
s04 fits the encoder on the TRAIN-split GUDID texts only, so the test split never shapes the vocabulary,
and saves it; s05 loads the same file for the hand sheet. No labels are used in the fit.
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import Iterable

import numpy as np

from vigil_model.embedding_retrieval import TfidfEncoder, record_text
from vigil_model.same_device_features import DeviceSide


def side_text(side: DeviceSide) -> str:
    """One line per record; the UDI is never part of it."""
    return record_text(side.manufacturer_norm, side.device_name, side.model_raw, side.catalog_raw,
                       " ".join(side.product_codes))


def fit(gudid_texts: Iterable[str]) -> TfidfEncoder:
    enc = TfidfEncoder()
    enc.fit(sorted(set(gudid_texts)))         # distinct and sorted: the fit does not depend on row order
    return enc


def pair_cosines(enc: TfidfEncoder, maude_texts: list[str], gudid_texts: list[str],
                 batch: int = 20_000) -> np.ndarray:
    """Row-wise cosine. Rows are L2-normalized by the vectorizer, so the dot product is the cosine;
    an empty text has a zero vector and cosine 0. In batches: the whole label set at once is two
    sparse matrices of several GB (~300 n-grams per text), which ran WSL out of memory."""
    if len(maude_texts) != len(gudid_texts):
        raise ValueError("maude_texts and gudid_texts differ in length")
    out = np.empty(len(maude_texts), dtype=np.float64)
    for i in range(0, len(maude_texts), batch):
        a, b = enc.encode(maude_texts[i:i + batch]), enc.encode(gudid_texts[i:i + batch])
        out[i:i + batch] = np.asarray(a.multiply(b).sum(axis=1)).ravel()
    return out


def save(enc: TfidfEncoder, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(enc))


def load(path: Path) -> TfidfEncoder:
    """Only load files s04 wrote: pickle runs code from the file."""
    return pickle.loads(Path(path).read_bytes())
