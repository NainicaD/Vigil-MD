"""Score candidate MAUDE <-> GUDID pairs with a released bundle; write the MATCH rows as links.

Reads a parquet of candidate pairs with the label set's key_* columns (see REQUIRED), scores it
in chunks with the same features as training (s04.device_sides + pair_features) and the bundle's
IE gate and cutoff (predict.Bundle.decide), optionally in several processes, and writes only the
MATCH rows, in the link-table shape of Data-Pipeline/scripts/warehouse/schemas.py LINK_COLUMNS.

Memory stays at about one chunk per process, whatever the number of pairs (prod: ~37 million):
the candidates are read chunk by chunk, each chunk's MATCH rows are appended to --out, and the
report comes from running totals. With --workers N at most 2N chunks are in flight.

File in, file out: no BigQuery. Not done here: building the candidate pairs, loading the output
into vigil_md_prod (a pipeline step; this folder writes only to vigil_md_model), and keeping
NON_MATCH / INSUFFICIENT_EVIDENCE rows.

--work-dir DIR: each scored chunk is saved to DIR/parts (temp file + rename) and its numbers
(pairs, seconds, pairs/s, decisions, peak memory) are appended to DIR/chunks.jsonl as it
finishes. A rerun with the same candidates, --batch, bundle and gate scores only the missing
chunks; other input stops with an error (DIR/parts/source.json). The DAG uses this, so a retry continues.

--ie-gate VERSION: apply this IE gate instead of the one the bundle names in meta.json (e.g. the
M1 gate on the published v1.1.0 bundle, before a bundle with M1 is released). The report records it.

Only point --bundle at bundles from the team's registry: tfidf.pkl is a pickle.

Usage (from anywhere):
    python scripts/s12_score_pairs.py --pairs data/candidates.parquet \
        --bundle models/final_v1 --out data/links_maude_gudid_model.parquet --workers 4
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import resource
import sys
import time
from collections import Counter, deque
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.s04_build_feature_table import device_sides, read_siblings  # noqa: E402
from vigil_model import predict, text_cosine  # noqa: E402
from vigil_model.rows import load_aliases, number, text  # noqa: E402
from vigil_model.same_device_features import IE_GATE_VERSIONS, MatchContext, pair_features  # noqa: E402

REQUIRED = ["maude_id", "gudid_id", "meta_n_dis_for_model",
            "key_m_mfr", "key_m_name", "key_m_model_raw", "key_m_catalog_raw", "key_m_pc",
            "key_g_mfr", "key_g_name", "key_g_model_raw", "key_g_catalog_raw", "key_g_pcs"]
# Same names and order as schemas.LINK_COLUMNS in Data-Pipeline; change both together.
LINK_COLUMNS = ["link_type", "left_source", "left_id", "right_source", "right_id", "link_key",
                "event_id", "same_event", "method", "score", "match_kind", "built_at"]
# Fixed types, so a chunk with no MATCH (empty or all-null columns) appends to the same file.
LINK_SCHEMA = pa.schema([(c, pa.string()) for c in LINK_COLUMNS[:7]] + [
    ("same_event", pa.bool_()), ("method", pa.string()), ("score", pa.float64()),
    ("match_kind", pa.string()), ("built_at", pa.timestamp("us", tz="UTC"))])
DECISIONS = (predict.MATCH, predict.NON_MATCH, predict.IE)
QUANTILE_BINS = 1000      # report quantiles from a histogram: exact to 1/1000 without keeping every score

_state: dict = {}     # per process: bundle, aliases, siblings (loaded once by _init)


def _init(bundle_dir, alias_path, siblings_path, ie_gate: str | None = None) -> None:
    _state["bundle"] = predict.load(bundle_dir)
    if ie_gate:
        _state["bundle"].meta["ie_gate"] = ie_gate
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


def _rss_mb() -> float:
    """Peak memory of this process so far (Linux reports ru_maxrss in KB)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def chunk_stat(i: int, n: int, out: pd.DataFrame, seconds: float, rss_mb: float | None,
               resumed: bool) -> dict:
    """One chunk's numbers: the chunks.jsonl line and the task-log line."""
    counts = out["decision"].value_counts()
    return {"chunk": i, "n_chunks": n, "pairs": len(out), "seconds": round(seconds, 3),
            "pairs_per_s": round(len(out) / seconds, 1) if seconds else None,
            **{f"n_{d.lower()}": int(counts.get(d, 0)) for d in DECISIONS},
            "max_rss_mb": rss_mb, "resumed": resumed}


def _score_job(job) -> tuple[int, pd.DataFrame, float, float]:
    """Score one chunk; with a part path, save it first (temp file + rename: never half-written)."""
    i, chunk, part = job
    t = time.monotonic()
    out = score_chunk(chunk)
    seconds = time.monotonic() - t
    if part is not None:
        tmp = part.with_suffix(".tmp")
        out.to_parquet(tmp)
        tmp.replace(part)
    return i, out, seconds, _rss_mb()


def _check_source(parts: Path, source: dict) -> None:
    """Parts may only be reused for the same candidates, batch size, bundle and gate."""
    f = parts / "source.json"
    if f.exists() and json.loads(f.read_text()) != source:
        raise SystemExit(f"{parts} holds checkpoints of other input (candidates, --batch, bundle or "
                         f"gate changed). Delete that folder to score from the start.")
    parts.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(source))


def read_chunks(path: Path, batch: int) -> tuple[int, int | None, "iter"]:
    """(number of chunks, meta_n_pool_rows or None, generator of DataFrames of exactly `batch`
    rows, the last one shorter). Reads the file batch by batch: never the whole file."""
    pf = pq.ParquetFile(path)
    missing = [c for c in REQUIRED if c not in pf.schema_arrow.names]
    if missing:
        raise SystemExit(f"candidate pairs lack columns: {', '.join(missing)}")
    n_rows = pf.metadata.num_rows
    if n_rows == 0:
        raise SystemExit("no candidate pairs to score")
    pool = None
    if "meta_n_pool_rows" in pf.schema_arrow.names:   # from s13: the same value on every row
        pool = int(pf.read_row_group(0, columns=["meta_n_pool_rows"]).column(0)[0].as_py())

    def chunks():
        # iter_batches stops at row-group ends: regroup, so chunk i is the same rows on every run
        buf, size, start = [], 0, 0
        for rb in pf.iter_batches(batch_size=batch):
            buf.append(rb)
            size += rb.num_rows
            while size >= batch:
                t = pa.Table.from_batches(buf)
                yield t.slice(0, batch).to_pandas().set_axis(range(start, start + batch))
                rest = t.slice(batch)
                buf, size, start = rest.to_batches(), rest.num_rows, start + batch
        if size:
            yield pa.Table.from_batches(buf).to_pandas().set_axis(range(start, start + size))
    return math.ceil(n_rows / batch), pool, chunks()


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


class Totals:
    """Running totals of every scored chunk: the run report without keeping every decision."""

    def __init__(self):
        self.n = 0
        self.decisions = Counter()
        self.hist10 = np.zeros(10, dtype=np.int64)
        self.fine = np.zeros(QUANTILE_BINS, dtype=np.int64)
        self.kinds = Counter()
        self.maude_ids = []       # unique 64-bit hashes per chunk; deduplicated once at the end

    def add(self, out: pd.DataFrame) -> None:
        scores = out["score"].to_numpy(float)
        self.n += len(out)
        self.decisions.update(out["decision"].tolist())
        self.hist10 += np.histogram(scores, bins=10, range=(0, 1))[0]
        self.fine += np.histogram(scores, bins=QUANTILE_BINS, range=(0, 1))[0]
        self.kinds.update(out.loc[out["decision"] == predict.MATCH, "match_kind"].tolist())
        self.maude_ids.append(np.unique(pd.util.hash_pandas_object(out["left_id"], index=False).to_numpy()))

    def quantile(self, q: float) -> float:
        """The middle of the 1/1000 bin that holds the q-quantile."""
        k = int(np.searchsorted(np.cumsum(self.fine), q * self.n, side="left"))
        return round((min(k, QUANTILE_BINS - 1) + 0.5) / QUANTILE_BINS, 4)

    def report(self, meta: dict, ie_gate: str, seconds: float, pool: int | None = None,
               baseline: dict | None = None, stats: list[dict] | None = None) -> dict:
        """One run's numbers for ELK: decisions, score distribution, match kinds, bundle, gate, and
        (with the chunk stats) throughput and peak memory of the chunks scored in this run."""
        report = {
            "n_pairs": self.n,
            "n_maude_rows": int(np.unique(np.concatenate(self.maude_ids)).size) if self.maude_ids else 0,
            **{f"n_{d.lower()}": int(self.decisions.get(d, 0)) for d in DECISIONS},
            **{f"rate_{d.lower()}": float(self.decisions.get(d, 0) / self.n) for d in DECISIONS},
            **{f"score_p{q}": self.quantile(q / 100) for q in (10, 50, 90)},
            "score_hist": self.hist10.tolist(),
            "match_kinds": dict(self.kinds.most_common()),
            "bundle_version": meta.get("version", "unknown"), "cutoff": float(meta["cutoff"]),
            "ie_gate": ie_gate, "seconds": round(seconds, 1),
        }
        if pool is not None:      # from s13: MAUDE rows in the pool, with or without candidates
            report["n_pool_rows"] = pool
        if baseline is not None:
            report["score_psi"] = psi(report["score_hist"], baseline["score_hist"])
        if stats:
            new = [s for s in stats if not s["resumed"]]
            busy = sum(s["seconds"] for s in new)
            report.update({
                "n_chunks": len(stats), "n_chunks_resumed": len(stats) - len(new),
                # per worker: chunk time is measured inside each worker process
                "pairs_per_s": round(sum(s["pairs"] for s in new) / busy, 1) if busy else None,
                "chunk_seconds_max": max((s["seconds"] for s in new), default=None),
                "peak_rss_mb": max((s["max_rss_mb"] for s in new), default=None)})
        return report


def score(pairs_path: Path, bundle_dir, alias_path, siblings_path, out: Path, built_at: datetime,
          batch: int = 100_000, workers: int = 1, work_dir: Path | None = None,
          source: dict | None = None, ie_gate: str | None = None) -> tuple[Totals, list[dict], int | None]:
    """Score every pair chunk by chunk; append each chunk's MATCH rows to `out` (links parquet).
    Returns the running totals, one stat per chunk, and meta_n_pool_rows. With work_dir: each chunk
    is saved to work_dir/parts (a rerun scores only the missing chunks) and its stat is appended
    to work_dir/chunks.jsonl as it finishes."""
    n, pool, chunks = read_chunks(pairs_path, batch)
    parts = Path(work_dir) / "parts" if work_dir else None
    log = None
    if parts:
        _check_source(parts, source or {})
        log = (Path(work_dir) / "chunks.jsonl").open("w")
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = pq.ParquetWriter(out, LINK_SCHEMA)
    totals, stats = Totals(), []

    def record(stat: dict, scored: pd.DataFrame) -> None:
        totals.add(scored)
        writer.write_table(pa.Table.from_pandas(to_links(scored, built_at), schema=LINK_SCHEMA,
                                                preserve_index=False))
        stats.append(stat)
        how = ("resumed from checkpoint" if stat["resumed"]
               else f"{stat['seconds']:.1f} s, peak {stat['max_rss_mb']} MB")
        print(f"chunk {stat['chunk'] + 1}/{n}: {stat['pairs']:,} pairs, {stat['n_match']:,} MATCH, {how}",
              flush=True)
        if log:
            log.write(json.dumps(stat) + "\n")
            log.flush()

    def jobs():
        """(i, saved result or None, job) in chunk order; a saved part needs no scoring."""
        for i, c in enumerate(chunks):
            part = parts / f"part-{i:05d}.parquet" if parts else None
            if part is not None and part.exists():
                yield i, pd.read_parquet(part), None
            else:
                yield i, None, (i, c, part)

    init = (bundle_dir, alias_path, siblings_path, ie_gate)
    try:
        # shortcut: processes on one machine only; add --shard i/N when a backfill needs several machines
        if workers <= 1:
            for i, saved, job in jobs():
                if saved is not None:
                    record(chunk_stat(i, n, saved, 0.0, None, True), saved)
                    continue
                if "bundle" not in _state or _state.get("init") != init:
                    _init(*init)
                    _state["init"] = init
                _, scored, sec, rss = _score_job(job)
                record(chunk_stat(i, n, scored, sec, rss, False), scored)
        else:
            # spawn, not fork: a fork after XGBoost/OpenMP threads have started deadlocks the workers
            with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                     initializer=_init, initargs=init) as pool_:
                inflight: deque = deque()

                def finish_oldest() -> None:
                    i, saved, fut = inflight.popleft()
                    if saved is not None:
                        record(chunk_stat(i, n, saved, 0.0, None, True), saved)
                    else:
                        _, scored, sec, rss = fut.result()
                        record(chunk_stat(i, n, scored, sec, rss, False), scored)

                for i, saved, job in jobs():
                    inflight.append((i, saved, None if job is None else pool_.submit(_score_job, job)))
                    while len(inflight) >= 2 * workers:      # bounded: never all chunks at once
                        finish_oldest()
                while inflight:
                    finish_oldest()
    finally:
        writer.close()
        if log:
            log.close()
    return totals, stats, pool


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
    ap.add_argument("--work-dir", type=Path,
                    help="checkpoints (parts/) and chunk stats (chunks.jsonl): a rerun continues")
    ap.add_argument("--ie-gate", choices=IE_GATE_VERSIONS,
                    help="apply this IE gate instead of the bundle's (meta.json ie_gate)")
    a = ap.parse_args(argv)

    started = time.monotonic()
    siblings = a.siblings
    if not siblings.exists():
        print(f"WARNING: {siblings} not found; scoring without sibling keys", file=sys.stderr)
        siblings = None
    meta = json.loads((a.bundle / "meta.json").read_text())
    gate = a.ie_gate or meta.get("ie_gate", "e6_e3b")      # v1.0.0 and older name no gate: e6_e3b
    st = a.pairs.stat()
    source = {"pairs_bytes": st.st_size, "pairs_mtime_ns": st.st_mtime_ns, "batch": a.batch,
              "bundle_version": meta.get("version"), "cutoff": meta["cutoff"], "ie_gate": gate}
    totals, stats, pool = score(a.pairs, a.bundle, a.alias, siblings, a.out, datetime.now(timezone.utc),
                                a.batch, a.workers, a.work_dir, source, a.ie_gate)
    print(f"{totals.n:,} pairs -> {dict(totals.decisions)}; cutoff {meta['cutoff']}, "
          f"version {meta.get('version', 'unknown')}, gate {gate}; "
          f"{totals.decisions.get(predict.MATCH, 0):,} links written to {a.out}")
    if a.report:
        baseline = json.loads(a.baseline.read_text()) if a.baseline else None
        report = totals.report(meta, gate, time.monotonic() - started, pool, baseline, stats)
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps(report, indent=2))
        if report.get("score_psi", 0) > PSI_WARN:
            print(f"WARNING: score_psi {report['score_psi']:.3f} > {PSI_WARN}: the score "
                  f"distribution moved against {a.baseline}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
