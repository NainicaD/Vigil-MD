"""Score candidate MAUDE <-> GUDID pairs with a released bundle; write the MATCH rows as links.

Reads a parquet of candidate pairs with the label set's key_* columns (see REQUIRED), scores it
in chunks with the same features as training (s04.device_sides + pair_features) and the bundle's
IE gate and cutoff (predict.Bundle.decide), optionally in several processes, and writes only the
MATCH rows, in the link-table shape of Data-Pipeline/scripts/warehouse/schemas.py LINK_COLUMNS.

File in, file out: no BigQuery. Not done here: building the candidate pairs, loading the output
into vigil_md_prod (a pipeline step; this folder writes only to vigil_md_model), and keeping
NON_MATCH / INSUFFICIENT_EVIDENCE rows.

Only point --bundle at bundles from the team's registry: tfidf.pkl is a pickle.

Usage (from anywhere):
    python scripts/s12_score_pairs.py --pairs data/candidates.parquet \
        --bundle models/final_v1 --out data/links_maude_gudid_model.parquet --workers 4
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.s04_build_feature_table import device_sides, read_siblings  # noqa: E402
from vigil_model import predict, text_cosine  # noqa: E402
from vigil_model.rows import load_aliases, number, text  # noqa: E402
from vigil_model.same_device_features import MatchContext, pair_features  # noqa: E402

REQUIRED = ["maude_id", "gudid_id", "meta_n_dis_for_model",
            "key_m_mfr", "key_m_name", "key_m_model_raw", "key_m_catalog_raw", "key_m_pc",
            "key_g_mfr", "key_g_name", "key_g_model_raw", "key_g_catalog_raw", "key_g_pcs"]
# Same names and order as schemas.LINK_COLUMNS in Data-Pipeline; change both together.
LINK_COLUMNS = ["link_type", "left_source", "left_id", "right_source", "right_id", "link_key",
                "event_id", "same_event", "method", "score", "match_kind", "built_at"]

_state: dict = {}     # per process: bundle, aliases, siblings (loaded once by _init)


def _init(bundle_dir, alias_path, siblings_path) -> None:
    _state["bundle"] = predict.load(bundle_dir)
    _state["aliases"] = load_aliases(alias_path)
    _state["siblings"] = read_siblings(siblings_path) if siblings_path else {}


def score_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Features, decision and score for every row of one chunk, in row order."""
    bundle, aliases, siblings = _state["bundle"], _state["aliases"], _state["siblings"]
    records = chunk.to_dict("records")
    sides = [device_sides(r) for r in records]
    cosines = np.round(text_cosine.pair_cosines(bundle.encoder,
                                                [text_cosine.side_text(m) for m, _ in sides],
                                                [text_cosine.side_text(g) for _, g in sides]), 6)
    feats = pd.DataFrame([
        pair_features(m, g, MatchContext(alias_pairs=aliases,
                                         n_dis_for_model=number(r["meta_n_dis_for_model"]),
                                         sibling_keys=siblings.get(text(r["key_g_mfr"]) or "", frozenset()),
                                         text_cosine=float(c)))
        for r, (m, g), c in zip(records, sides, cosines)], index=chunk.index)
    out = bundle.decide(feats)
    out["match_kind"] = feats["feat_match_kind"]
    out["left_id"], out["right_id"] = chunk["maude_id"], chunk["gudid_id"]
    return out


def score(pairs: pd.DataFrame, bundle_dir, alias_path, siblings_path,
          batch: int = 100_000, workers: int = 1) -> pd.DataFrame:
    """Every pair scored and decided (score, decision, match_kind, left_id, right_id)."""
    missing = [c for c in REQUIRED if c not in pairs.columns]
    if missing:
        raise SystemExit(f"candidate pairs lack columns: {', '.join(missing)}")
    if pairs.empty:
        raise SystemExit("no candidate pairs to score")
    chunks = [pairs.iloc[i:i + batch] for i in range(0, len(pairs), batch)]
    init = (bundle_dir, alias_path, siblings_path)
    # shortcut: processes on one machine only; add --shard i/N when a backfill needs several machines
    if workers <= 1:
        _init(*init)
        parts = [score_chunk(c) for c in chunks]
    else:
        # spawn, not fork: a fork after XGBoost/OpenMP threads have started deadlocks the workers
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_init, initargs=init) as pool:
            parts = list(pool.map(score_chunk, chunks))     # map keeps chunk order
    return pd.concat(parts)


def to_links(scored: pd.DataFrame, built_at: datetime) -> pd.DataFrame:
    """The MATCH rows as link rows (LINK_COLUMNS)."""
    m = scored[scored["decision"] == predict.MATCH]
    # typed nulls: an all-None parquet column has no type, and BigQuery may refuse to load it
    null = {"string": pd.Series(pd.NA, index=m.index, dtype="string"),
            "boolean": pd.Series(pd.NA, index=m.index, dtype="boolean")}
    links = pd.DataFrame({
        "link_type": "maude_gudid", "left_source": "maude", "left_id": m["left_id"],
        "right_source": "gudid", "right_id": m["right_id"], "link_key": null["string"],
        "event_id": null["string"], "same_event": null["boolean"], "method": "model",
        "score": m["score"].astype(float),
        "match_kind": m["match_kind"], "built_at": built_at}, index=m.index)
    return links[LINK_COLUMNS].reset_index(drop=True)


PSI_WARN = 0.2     # common rule of thumb: above 0.2 the score distribution moved a lot


def psi(hist: list[int], baseline: list[int], eps: float = 1e-4) -> float:
    """Population stability index of two histograms with the same bins."""
    p = np.clip(np.asarray(hist, float) / max(sum(hist), 1), eps, None)
    q = np.clip(np.asarray(baseline, float) / max(sum(baseline), 1), eps, None)
    return float(np.sum((p - q) * np.log(p / q)))


def run_report(scored: pd.DataFrame, pairs: pd.DataFrame, meta: dict, seconds: float,
               baseline: dict | None = None) -> dict:
    """One run's numbers for ELK: decisions, score distribution, match kinds, bundle."""
    counts = scored["decision"].value_counts()
    scores = scored["score"].to_numpy(float)
    report = {
        "n_pairs": len(scored), "n_maude_rows": int(scored["left_id"].nunique()),
        **{f"n_{d.lower()}": int(counts.get(d, 0)) for d in (predict.MATCH, predict.NON_MATCH, predict.IE)},
        **{f"rate_{d.lower()}": float(counts.get(d, 0) / len(scored))
           for d in (predict.MATCH, predict.NON_MATCH, predict.IE)},
        **{f"score_p{q}": float(np.quantile(scores, q / 100)) for q in (10, 50, 90)},
        "score_hist": np.histogram(scores, bins=10, range=(0, 1))[0].tolist(),
        "match_kinds": scored.loc[scored["decision"] == predict.MATCH, "match_kind"].value_counts().to_dict(),
        "bundle_version": meta.get("version", "unknown"), "cutoff": float(meta["cutoff"]),
        "seconds": round(seconds, 1),
    }
    if "meta_n_pool_rows" in pairs.columns:      # from s13: MAUDE rows in the pool, with or without candidates
        report["n_pool_rows"] = int(pairs["meta_n_pool_rows"].iloc[0])
    if baseline is not None:
        report["score_psi"] = psi(report["score_hist"], baseline["score_hist"])
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", type=Path, required=True, help="candidate pairs parquet")
    ap.add_argument("--bundle", type=Path, required=True, help="released bundle folder (s11)")
    ap.add_argument("--alias", type=Path, default=ROOT / "labels/alias_table_v1.csv")
    ap.add_argument("--siblings", type=Path, default=ROOT / "data/sibling_keys.csv")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--batch", type=int, default=100_000, help="pairs per chunk")
    ap.add_argument("--workers", type=int, default=1, help="processes; 1 runs inline")
    ap.add_argument("--report", type=Path, help="write the run's numbers here (JSON, for ELK)")
    ap.add_argument("--baseline", type=Path, help="an earlier --report: adds score_psi")
    a = ap.parse_args(argv)

    started = time.monotonic()
    siblings = a.siblings
    if not siblings.exists():
        print(f"WARNING: {siblings} not found; scoring without sibling keys", file=sys.stderr)
        siblings = None
    pairs = pd.read_parquet(a.pairs)      # rows.text cleans nulls; parquet keeps model numbers as text
    scored = score(pairs, a.bundle, a.alias, siblings, a.batch, a.workers)
    links = to_links(scored, datetime.now(timezone.utc))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    links.to_parquet(a.out, index=False)
    meta = json.loads((a.bundle / "meta.json").read_text())
    print(f"{len(pairs):,} pairs -> {scored['decision'].value_counts().to_dict()}; "
          f"cutoff {meta['cutoff']}, version {meta.get('version', 'unknown')}; "
          f"{len(links):,} links written to {a.out}")
    if a.report:
        baseline = json.loads(a.baseline.read_text()) if a.baseline else None
        report = run_report(scored, pairs, meta, time.monotonic() - started, baseline)
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(report, indent=2))
        if report.get("score_psi", 0) > PSI_WARN:
            print(f"WARNING: score_psi {report['score_psi']:.3f} > {PSI_WARN}: the score "
                  f"distribution moved against {a.baseline}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
