"""Embedding retrieval prototype: find the right GUDID model group for a MAUDE record.

Reads the export of scripts/export_embedding_data.py (data/embedding/<seed>/), turns every GUDID
model group and every MAUDE tuple into one line of text, embeds both sides with each method, and
for every MAUDE tuple takes the k nearest GUDID groups (cosine similarity). The UDI link is the
answer, so each method is scored without hand labels:

  Recall@k  share of MAUDE tuples whose linked GUDID group is in the top k
  MRR       mean of 1 / rank of the linked group (0 if it is not in the top k)

Methods (--methods, several allowed):
  tfidf                 character n-grams (3-5) with TF-IDF. Baseline, no download.
  st:<model name>       a sentence-transformers model, e.g. st:sentence-transformers/all-MiniLM-L6-v2

Outputs in reports/embedding/<run name>/:
  metrics.json          overall scores and timing per method
  slices.csv            Recall@1/10 per slice (tier, id_agrees, mfr_equal, has_identifier) with a
                        Wilson 95% interval and the disparity flag used in docs/BIAS_REPORT.md
  misses_<method>.csv   50 tuples the method missed, with the right answer and its top 3
  comparison.md         one table comparing the methods
Embeddings are cached in data/embedding/<seed>/cache/, so a re-run of a method is fast.

Usage (from Model-Development/matching/):
  $PY scripts/embedding_retrieval.py --data data/embedding/vigil-emb-v1 --methods tfidf
  $PY scripts/embedding_retrieval.py --data data/embedding/vigil-emb-v1 \
      --methods tfidf st:sentence-transformers/all-MiniLM-L6-v2 st:BAAI/bge-small-en-v1.5
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports" / "embedding"
KS = (1, 5, 10, 50)
ALL_FIELDS = ("mfr", "name", "model", "catalog", "pc")
SLICE_COLS = ("tier", "id_agrees", "mfr_equal", "has_identifier")
MISSES_TO_SAVE = 50

# Same rule as the README's shared conventions (and the SQL): key = upper, strip spaces - _ . /
PLACEHOLDER = re.compile(r"^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE"
                         r"|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$")
KEY_STRIP = re.compile(r"[\s\-_./]")

log = logging.getLogger("embedding_retrieval")


# ---------------------------------------------------------------------------- text

def clean(value) -> str:
    """Trim, collapse spaces, lower-case. Missing values become ''."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def usable_identifier(value) -> str:
    """The identifier as written (cleaned), or '' if it is a placeholder (E6)."""
    text = clean(value)
    key = KEY_STRIP.sub("", text).upper()
    if len(key) < 2 or PLACEHOLDER.match(key):
        return ""
    return text


def record_text(mfr, name, model, catalog, product_code, fields=ALL_FIELDS) -> str:
    """One line per record. Both sides use the same template so the texts are comparable.
    The UDI is never part of the text: it is the answer."""
    catalogs = [usable_identifier(c) for c in clean(catalog).split(";")]
    parts = {
        "mfr": clean(mfr),
        "name": clean(name),
        "model": usable_identifier(model),
        "catalog": " ".join(dict.fromkeys(c for c in catalogs if c)),   # unique, in order
        "pc": clean(product_code).replace(";", " "),
    }
    labels = {"mfr": "", "name": "", "model": "model ", "catalog": "catalog ", "pc": "code "}
    return " | ".join(labels[f] + parts[f] for f in fields if parts[f])


def gudid_texts(groups: pd.DataFrame, fields=ALL_FIELDS) -> list[str]:
    return [record_text(r.mfr or r.manufacturer_name_raw, r.device_name_raw, r.model_number_raw,
                        r.catalog_examples or r.catalog_number, r.product_codes, fields)
            for r in groups.itertuples()]


def maude_texts(queries: pd.DataFrame, fields=ALL_FIELDS) -> list[str]:
    return [record_text(r.mfr or r.manufacturer_name_raw, r.device_name_raw, r.model_number_raw,
                        r.catalog_number, r.product_code, fields)
            for r in queries.itertuples()]


# ---------------------------------------------------------------------------- encoders

class TfidfEncoder:
    """Character n-grams. Fitted on the GUDID side only (no labels are used)."""

    def __init__(self, ngram_range=(3, 5)):
        self.name = "tfidf"
        self.ngram_range = ngram_range
        self.vec = None

    def fit(self, texts: list[str]) -> None:
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=self.ngram_range, min_df=2,
                                   sublinear_tf=True, dtype=np.float32).fit(texts)

    def encode(self, texts: list[str]):
        return self.vec.transform(texts)          # sparse, rows already L2-normalized


class SentenceTransformerEncoder:
    """A pretrained sentence-transformers model. Vectors are L2-normalized."""

    def __init__(self, model_name: str, batch_size: int = 128, device: str = "auto"):
        self.name = f"st:{model_name}"
        self.model_name = model_name
        self.batch_size = batch_size
        self.device = device
        # e5 models expect a prefix on every text; "query: " on both sides for record matching
        self.prefix = "query: " if "e5" in model_name.lower() else ""
        self.model = None

    def fit(self, texts: list[str]) -> None:
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(self.model_name, device=pick_device(self.device))

    def encode(self, texts: list[str]) -> np.ndarray:
        emb = self.model.encode([self.prefix + t for t in texts], batch_size=self.batch_size,
                                normalize_embeddings=True, convert_to_numpy=True,
                                show_progress_bar=True)
        return emb.astype(np.float32)


def pick_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():     # Apple silicon
            return "mps"
    except ImportError:
        pass
    return "cpu"


def make_encoder(method: str, batch_size: int, device: str):
    if method == "tfidf":
        return TfidfEncoder()
    if method.startswith("st:"):
        return SentenceTransformerEncoder(method[3:], batch_size, device)
    raise ValueError(f"unknown method {method!r} (use tfidf or st:<model name>)")


# ---------------------------------------------------------------------------- search

def topk_search(Q, X, k: int, max_cells: int = 50_000_000) -> tuple[np.ndarray, np.ndarray]:
    """Top-k columns of Q @ X.T per row, best first. Q and X are row-normalized (dense or
    scipy sparse), so the scores are cosine similarities. Works in chunks of queries so that at
    most max_cells scores are in memory at once (50M floats = 200 MB)."""
    n_q, n_x = Q.shape[0], X.shape[0]
    k = min(k, n_x)
    XT = X.T.tocsr() if hasattr(X, "tocsr") else X.T
    chunk = max(1, max_cells // max(n_x, 1))
    idx = np.empty((n_q, k), dtype=np.int64)
    scores = np.empty((n_q, k), dtype=np.float32)
    for start in range(0, n_q, chunk):
        S = Q[start:start + chunk] @ XT
        S = np.asarray(S.toarray() if hasattr(S, "toarray") else S, dtype=np.float32)
        part = np.argpartition(-S, k - 1, axis=1)[:, :k]
        part_scores = np.take_along_axis(S, part, axis=1)
        # best first; equal scores are ordered by index so results are reproducible
        order = np.lexsort((part, -part_scores), axis=1)
        idx[start:start + chunk] = np.take_along_axis(part, order, axis=1)
        scores[start:start + chunk] = np.take_along_axis(part_scores, order, axis=1)
    return idx, scores


# ---------------------------------------------------------------------------- metrics

def truth_positions(queries: pd.DataFrame, groups: pd.DataFrame) -> list[set[int]]:
    pos = {g: i for i, g in enumerate(groups["group_key"])}
    return [{pos[g] for g in str(keys).split(";") if g in pos} for keys in queries["true_group_keys"]]


def first_hit_rank(topk_idx: np.ndarray, truths: list[set[int]]) -> np.ndarray:
    """1-based rank of the first correct group in each row's top k; 0 if none."""
    ranks = np.zeros(len(truths), dtype=np.int64)
    for i, (row, truth) in enumerate(zip(topk_idx, truths)):
        for r, j in enumerate(row, start=1):
            if j in truth:
                ranks[i] = r
                break
    return ranks


def retrieval_metrics(ranks: np.ndarray, ks=KS) -> dict[str, float]:
    found = ranks > 0
    out = {f"recall@{k}": float(np.mean(found & (ranks <= k))) if len(ranks) else 0.0 for k in ks}
    out["mrr"] = float(np.mean(np.where(found, 1.0 / np.maximum(ranks, 1), 0.0))) if len(ranks) else 0.0
    return out


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def slice_table(method: str, ranks: np.ndarray, queries: pd.DataFrame, k: int = 10) -> list[dict]:
    """Recall@1 and Recall@k per slice value. A slice is flagged as in bias_slices.py:
    n >= 30, |gap| >= 10 points, and its 95% interval excludes the overall rate."""
    hit_k = (ranks > 0) & (ranks <= k)
    hit_1 = ranks == 1
    overall = float(hit_k.mean()) if len(ranks) else 0.0
    rows = []
    for col in SLICE_COLS:
        if col not in queries:
            continue
        for value, mask in queries.groupby(queries[col].astype(str)).indices.items():
            n = len(mask)
            r = float(hit_k[mask].mean())
            lo, hi = wilson(int(hit_k[mask].sum()), n)
            gap = 100 * (r - overall)
            rows.append({
                "method": method, "slice": col, "value": value, "n": n,
                "recall@1": round(float(hit_1[mask].mean()), 4),
                f"recall@{k}": round(r, 4), "ci_low": round(lo, 4), "ci_high": round(hi, 4),
                "gap_pp": round(gap, 2),
                "disparity_flag": bool(n >= 30 and abs(gap) >= 10 and not (lo <= overall <= hi)),
            })
    return rows


def misses(method, ranks, topk_idx, queries, q_texts, g_texts, groups, truths, n=MISSES_TO_SAVE,
           seed=0) -> pd.DataFrame:
    """A fixed random sample of tuples whose answer is not in the top k, for reading by hand."""
    missed = np.flatnonzero(ranks == 0)
    if len(missed) > n:
        missed = np.sort(np.random.default_rng(seed).choice(missed, n, replace=False))
    rows = []
    for i in missed:
        truth = sorted(truths[i])
        rows.append({
            "method": method, "query_id": queries["query_id"].iat[i], "query_text": q_texts[i],
            "answer_text": g_texts[truth[0]] if truth else "",
            "answer_group_key": groups["group_key"].iat[truth[0]] if truth else "",
            **{f"top{r + 1}_text": g_texts[j] for r, j in enumerate(topk_idx[i][:3])},
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------- data

def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    read = dict(dtype=str, keep_default_na=False, na_values=[""])
    groups = pd.read_csv(data_dir / "gudid_groups.csv", **read)
    queries = pd.read_csv(data_dir / "maude_queries.csv", **read)
    labelers = pd.read_csv(data_dir / "labelers.csv", **read)
    for col in ("id_agrees", "mfr_equal", "has_identifier"):
        queries[col] = queries[col].str.lower().eq("true")
    # The tier of the labeler the answer belongs to (first one if a tuple links to several).
    tier = dict(zip(labelers["mfr"], labelers["tier"]))
    queries["tier"] = [tier.get(str(m).split(";")[0], "unknown") for m in queries["gudid_mfrs"]]
    # Missing values as None (pandas may use NaN, which is truthy and would read as text).
    groups = groups.astype(object).where(groups.notna(), None)
    queries = queries.astype(object).where(queries.notna(), None)
    return groups, queries


def cached_encode(encoder, texts: list[str], cache_dir: Path | None, side: str):
    """Embeddings of dense methods are cached by method and text content."""
    if cache_dir is None or encoder.name == "tfidf":
        return encoder.encode(texts)
    h = hashlib.sha256(("\n".join(texts) + "|" + encoder.name).encode()).hexdigest()[:16]
    path = cache_dir / f"{re.sub(r'[^A-Za-z0-9]+', '_', encoder.name)}_{side}_{h}.npy"
    if path.exists():
        log.info("cache hit: %s", path.name)
        return np.load(path)
    emb = encoder.encode(texts)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.save(path, emb)
    return emb


# ---------------------------------------------------------------------------- run

def evaluate(encoder, groups, queries, fields=ALL_FIELDS, k=max(KS), cache_dir=None):
    """Embed both sides, search, score. Returns (metrics, ranks, topk_idx, texts)."""
    g_texts, q_texts = gudid_texts(groups, fields), maude_texts(queries, fields)
    t0 = time.perf_counter()
    encoder.fit(g_texts)
    X = cached_encode(encoder, g_texts, cache_dir, "gudid")
    Q = cached_encode(encoder, q_texts, cache_dir, "maude")
    t_encode = time.perf_counter() - t0
    t0 = time.perf_counter()
    topk_idx, _ = topk_search(Q, X, k)
    t_search = time.perf_counter() - t0
    truths = truth_positions(queries, groups)
    ranks = first_hit_rank(topk_idx, truths)
    metrics = {**retrieval_metrics(ranks), "n_queries": len(queries), "n_index": len(groups),
               "encode_seconds": round(t_encode, 1), "search_seconds": round(t_search, 1)}
    return metrics, ranks, topk_idx, (g_texts, q_texts, truths)


def comparison_md(results: dict[str, dict]) -> str:
    cols = [f"recall@{k}" for k in KS] + ["mrr", "encode_seconds"]
    lines = ["| method | " + " | ".join(cols) + " |", "|---|" + "---:|" * len(cols)]
    for m, r in results.items():
        cells = [f"{r[c]:.3f}" if c != "encode_seconds" else f"{r[c]:.0f}s" for c in cols]
        lines.append(f"| {m} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def log_mlflow(run_name, method, metrics, params, artifacts):
    import mlflow
    mlflow.set_experiment("vigil-embedding-retrieval")
    with mlflow.start_run(run_name=f"{run_name}:{method}"):
        mlflow.log_params({"method": method, **params})
        mlflow.log_metrics({k.replace("@", "_at_"): v for k, v in metrics.items()
                            if isinstance(v, (int, float))})
        for path in artifacts:
            mlflow.log_artifact(str(path))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", required=True, type=Path, help="data/embedding/<seed>")
    p.add_argument("--methods", nargs="+", default=["tfidf"])
    p.add_argument("--fields", default=",".join(ALL_FIELDS),
                   help=f"fields in the text, any of {','.join(ALL_FIELDS)} (for ablations)")
    p.add_argument("--max-queries", type=int, default=0, help="use only the first N tuples (0 = all)")
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--device", default="auto", help="auto, cpu, mps or cuda")
    p.add_argument("--run-name", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--mlflow", action="store_true", help="also log every method to MLflow")
    return p.parse_args(argv)


def main(argv=None, encoders=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    fields = tuple(f for f in args.fields.split(",") if f)
    bad = set(fields) - set(ALL_FIELDS)
    if bad:
        log.error("unknown fields: %s", sorted(bad))
        return 2
    groups, queries = load_data(args.data)
    if args.max_queries:
        queries = queries.head(args.max_queries).reset_index(drop=True)
    run_name = args.run_name or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = REPORTS / run_name
    out.mkdir(parents=True, exist_ok=True)
    log.info("index: %d GUDID groups; queries: %d MAUDE tuples; fields: %s",
             len(groups), len(queries), ",".join(fields))

    encoders = encoders or [make_encoder(m, args.batch_size, args.device) for m in args.methods]
    cache_dir = None if args.no_cache else args.data / "cache"
    results, slices = {}, []
    for enc in encoders:
        log.info("method %s ...", enc.name)
        metrics, ranks, topk_idx, (g_texts, q_texts, truths) = evaluate(
            enc, groups, queries, fields, cache_dir=cache_dir)
        results[enc.name] = metrics
        slices += slice_table(enc.name, ranks, queries)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", enc.name)
        miss_path = out / f"misses_{safe}.csv"
        misses(enc.name, ranks, topk_idx, queries, q_texts, g_texts, groups, truths).to_csv(
            miss_path, index=False)
        log.info("%s: %s", enc.name, {k: round(v, 4) if isinstance(v, float) else v
                                      for k, v in metrics.items()})
        if args.mlflow:
            log_mlflow(run_name, enc.name, metrics,
                       {"fields": ",".join(fields), "data": args.data.name,
                        "n_index": len(groups), "n_queries": len(queries)}, [miss_path])

    (out / "metrics.json").write_text(json.dumps({
        "run_name": run_name, "data": str(args.data), "fields": list(fields),
        "results": results}, indent=2), encoding="utf-8")
    pd.DataFrame(slices).to_csv(out / "slices.csv", index=False)
    table = comparison_md(results)
    (out / "comparison.md").write_text(table, encoding="utf-8")
    print("\n" + table)
    flagged = [s for s in slices if s["disparity_flag"]]
    for s in flagged:
        log.warning("disparity: %s %s=%s recall@10 %.3f (gap %+.1f pp, n=%d)", s["method"],
                    s["slice"], s["value"], s["recall@10"], s["gap_pp"], s["n"])
    log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
