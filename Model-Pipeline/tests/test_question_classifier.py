"""Tests for question_classifier: labels, guardrails, generator, metrics, train/predict/evaluate.

Training tests use a small generated set so the whole file runs in seconds on a CPU.
"""
import json
import random

import numpy as np
import pytest

from question_classifier import generate as gen
from question_classifier import guardrails as gr
from question_classifier.evaluate import evaluate
from question_classifier.labels import LABELS, OUT_OF_SCOPE, from_vector, to_vector
from question_classifier.metrics import OOS_MAX_THRESHOLD, score, tune_thresholds
from question_classifier.predict import QuestionClassifier
from question_classifier.templates import TEMPLATES, UNRELATED
from question_classifier.train import save, train

OOS = LABELS.index(OUT_OF_SCOPE)

# Written for the tests, not taken from the templates.
ADVICE = [
    "Should I stop using my insulin pump?",
    "is my pacemaker safe",
    "Is the Omnipod 5 a dangerous device?",
    "my cgm keeps falling off what should i do",
    "how many units of insulin do i take if the pump breaks",
    "Which defibrillator is the safest for kids?",
    "what's the best cpap on the market",
    "would you recommend this knee implant for my dad",
    "can i still wear my glucose monitor in an MRI",
    "I'm scared of my heart valve, is it risky",
    "should we switch our hospital's infusion pumps to Baxter",
    "I have a rash near my pump site",
]
FACTUAL = [
    "How many death reports does the t:slim X2 have since 2020?",
    "What did the recall recommend users do?",
    "what did the FDA advise in the Impella recall",
    "Was the DreamStation recalled and why?",
    "top problems reported for insulin pumps",
    "who makes UDI 00643169007222",
    "describe the injury reports for the HeartMate 3",
    "compare malfunction reports for Medtronic and Tandem pumps",
    "what class was the Alaris recall",
    "count of MDRs for product code LZG by year",
]


# ── labels ────────────────────────────────────────────────────────────────────

def test_label_vectors_round_trip():
    assert from_vector(to_vector(["recalls", "counts"])) == ["counts", "recalls"]
    with pytest.raises(ValueError):
        to_vector(["nope"])


# ── guardrails ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", ADVICE)
def test_rules_catch_medical_advice(q):
    assert gr.rule_hits(q), q


@pytest.mark.parametrize("q", FACTUAL)
def test_rules_leave_factual_questions_alone(q):
    assert gr.rule_hits(q) == [], q


def test_strict_policy_refuses_everything_with_fixed_text():
    d = gr.route(["counts"], "How many deaths has X had? Should I stop using it?")
    assert d["out_of_scope"] and d["run_queries"] == [] and d["refusal"] == gr.REFUSAL
    assert d["labels"] == ["counts", OUT_OF_SCOPE] and "should_i" in d["rule_hits"]


def test_facts_with_refusal_policy_keeps_factual_queries():
    d = gr.route(["counts", OUT_OF_SCOPE], "how many deaths, is it safe",
                 policy="facts_with_refusal")
    assert d["run_queries"] == ["counts"] and d["refusal"] == gr.REFUSAL


def test_in_scope_question_runs_its_queries():
    d = gr.route(["counts", "recalls"], "How many deaths, and was it recalled?")
    assert not d["out_of_scope"] and d["run_queries"] == ["counts", "recalls"]
    assert d["refusal"] is None


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        gr.route([], "x", policy="lenient")


def test_refusal_gives_no_advice_and_points_to_clinician():
    assert "doctor" in gr.REFUSAL and "can't give medical advice" in gr.REFUSAL


# ── generator ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def data():
    return gen.generate(n_train=900, n_val=300, seed=7)


def test_generation_is_deterministic():
    a = gen.generate(50, 20, seed=3)
    b = gen.generate(50, 20, seed=3)
    assert a == b


def test_name_pools_are_disjoint():
    pools = gen.split_pools(gen.load_slots(), 0.25, seed=7)
    for key in gen.SPLIT_SLOTS:
        assert not set(pools["train"][key]) & set(pools["val"][key]), key
        assert pools["val"][key] and pools["train"][key]


def test_validation_never_names_a_training_device(data):
    pools = gen.split_pools(gen.load_slots(), 0.25, seed=7)
    train_only = [d.lower() for d in pools["train"]["device"]]
    for row in data["val"]:
        text = row["text"].lower()
        assert not any(d in text for d in train_only), row["text"]


def test_templates_are_held_out_per_label():
    split = gen.split_templates(TEMPLATES, 0.25, seed=7)
    for label in LABELS:
        tr, va = set(split["train"][label]), set(split["val"][label])
        assert tr and va and not tr & va, label


def test_every_label_and_multi_label_present(data):
    rows = data["train"]
    assert {lbl for r in rows for lbl in r["labels"]} == set(LABELS)
    multi = sum(len(r["labels"]) == 2 for r in rows) / len(rows)
    assert 0.15 < multi < 0.35
    assert all(r["labels"] == sorted(r["labels"]) for r in rows)


def test_oos_kind_marks_advice_and_unrelated(data):
    for r in data["train"] + data["val"]:
        assert ("oos_kind" in r) == (OUT_OF_SCOPE in r["labels"])
    kinds = {r.get("oos_kind") for r in data["train"]}
    assert {"advice", "unrelated"} <= kinds
    assert "what is the weather today" in UNRELATED
    assert "should i stop using my {generic}" not in UNRELATED


def test_noise_keeps_text_non_empty():
    rng = random.Random(0)
    for _ in range(200):
        assert gen.add_noise("how many reports", rng).strip()


# ── metrics ───────────────────────────────────────────────────────────────────

def _toy(n=200, seed=0):
    rng = np.random.default_rng(seed)
    y = rng.integers(0, 2, size=(n, len(LABELS)))
    probs = np.clip(y * 0.7 + rng.random((n, len(LABELS))) * 0.4, 0, 1)
    return probs, y


def test_oos_threshold_is_capped():
    probs, y = _toy()
    probs[:, OOS] = np.where(y[:, OOS] == 1, 0.99, 0.01)       # perfectly separable
    assert tune_thresholds(probs, y)[OUT_OF_SCOPE] <= OOS_MAX_THRESHOLD


def test_unreachable_recall_does_not_fall_back_to_lowest_threshold():
    """A weak model can't reach 98% recall alone: pick by F2, not the lowest threshold
    (which would refuse many legitimate questions)."""
    rng = np.random.default_rng(1)
    probs, y = _toy()
    pos = y[:, OOS] == 1
    probs[:, OOS] = np.where(pos, rng.random(len(y)), rng.random(len(y)) * 0.4)
    assert tune_thresholds(probs, y)[OUT_OF_SCOPE] > 0.05


def test_rules_let_the_model_threshold_rise_to_the_cap():
    rng = np.random.default_rng(1)
    probs, y = _toy()
    pos = y[:, OOS] == 1
    probs[:, OOS] = np.where(pos, rng.random(len(y)), rng.random(len(y)) * 0.4)
    without = tune_thresholds(probs, y)[OUT_OF_SCOPE]
    with_rules = tune_thresholds(probs, y, rule_mask=pos)[OUT_OF_SCOPE]   # rules catch all
    assert with_rules == OOS_MAX_THRESHOLD and without < with_rules


def test_score_reports_false_refusals():
    y = np.zeros((4, len(LABELS)), int)
    y[:, 0] = 1
    pred = y.copy()
    pred[0, OOS] = 1
    m = score(y, pred)
    assert m["false_refusal_rate"] == 0.25 and m["per_label"]["counts"]["f1"] == 1.0


# ── train → predict → evaluate ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def model_dir(tmp_path_factory, data):
    pipe, meta = train(data["train"], data["val"], c_grid=(2.0,))
    out = tmp_path_factory.mktemp("model")
    save(pipe, meta, out)
    return out


def test_meta_is_complete(model_dir):
    meta = json.loads((model_dir / "meta.json").read_text())
    assert meta["labels"] == list(LABELS) and set(meta["thresholds"]) == set(LABELS)
    assert meta["val_metrics"]["n"] == 300


def test_classify_refuses_advice_even_if_model_misses(model_dir):
    clf = QuestionClassifier.load(model_dir)
    for q in ADVICE:
        out = clf.classify(q)
        assert out["out_of_scope"] and out["run_queries"] == [], q
        assert out["refusal"] == gr.REFUSAL


def test_classify_routes_factual_questions(model_dir):
    clf = QuestionClassifier.load(model_dir)
    out = clf.classify("How many death reports does the t:slim X2 have since 2020?")
    assert "counts" in out["run_queries"] and not out["out_of_scope"]
    assert set(out["scores"]) == set(LABELS) and out["unclear"] is False


def test_load_rejects_label_mismatch(model_dir, tmp_path):
    meta = json.loads((model_dir / "meta.json").read_text())
    meta["labels"] = ["counts"]
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "model.joblib").write_bytes((model_dir / "model.joblib").read_bytes())
    with pytest.raises(ValueError):
        QuestionClassifier.load(tmp_path)


def test_evaluate_reports_safety_numbers(model_dir):
    rows = ([{"text": q, "labels": [OUT_OF_SCOPE], "audience": "patient", "oos_kind": "advice"}
             for q in ADVICE]
            + [{"text": "what is the capital of peru", "labels": [OUT_OF_SCOPE],
                "audience": "neutral", "oos_kind": "unrelated"}]
            + [{"text": "How many death reports does the t:slim X2 have since 2020?",
                "labels": ["counts"], "audience": "researcher"}])
    r = evaluate(QuestionClassifier.load(model_dir), rows)
    assert r["advice_recall"] == 1.0 and r["missed_advice"] == []
    assert sum(r["unrelated"].values()) == 1
    assert set(r["slices"]) == {"patient", "neutral", "researcher"}
    assert r["rule_false_alarms"]["of_in_scope"] == 1
