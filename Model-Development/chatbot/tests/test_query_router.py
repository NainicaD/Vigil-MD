"""Tests for query_router: labels, guardrails, generator, metrics, train/predict/evaluate.

Training tests use a small generated set so the whole file runs in seconds on a CPU.
"""
import json
import random
from pathlib import Path

import numpy as np
import pytest

from query_router import generate as gen
from query_router import guardrails as gr
from query_router.evaluate import evaluate, evaluate_false_alarms
from query_router.labels import LABELS, OUT_OF_SCOPE, from_vector, to_vector
from query_router.metrics import OOS_MAX_THRESHOLD, score, tune_thresholds
from query_router.predict import QueryRouter
from query_router.templates import TEMPLATES, UNRELATED
from query_router.train import save, train

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
    d = gr.route(["counts"], "How many deaths has X had? Should I stop using it?",
                 policy="strict")
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


# ── fixed replies: emergency line, scope, help ────────────────────────────────

FROZEN_RULES = "0dd7a4dd862a"      # the held-out numbers in README.md are for these rules


def test_advice_rules_stay_frozen():
    # The replies below must not change which questions are refused.
    assert gr.rules_fingerprint() == FROZEN_RULES


def test_refusal_has_an_emergency_line():
    assert "If this is an emergency, call 911" in gr.REFUSAL


@pytest.mark.parametrize("q", [
    "write me a poem about the ocean", "what is the capital of france", "tell me a joke",
    "write python code to sort a list"])
def test_unrelated_topic_gets_the_scope_reply(q):
    d = gr.route([OUT_OF_SCOPE], q)
    assert d["reply_kind"] == "scope" and d["reply"] == gr.SCOPE_REPLY
    assert d["refusal"] == gr.SCOPE_REPLY and d["run_queries"] == [] and d["out_of_scope"]


@pytest.mark.parametrize("q", [
    "I feel dizzy and weak",                      # health word, no rule
    "is Astral 150 trustworthy",                  # judgement word, model number
    "would it be crazy to keep my MiniMed 780G",  # model number
    "my cgm fell off again, now what",            # device word
    "what stocks should i buy",                   # an advice rule fires: safer reply wins
])
def test_out_of_scope_with_health_or_device_context_gets_the_medical_refusal(q):
    d = gr.route([OUT_OF_SCOPE], q)
    assert d["reply_kind"] == "medical_refusal" and d["reply"] == gr.REFUSAL


def test_out_of_scope_with_a_factual_label_gets_the_medical_refusal():
    d = gr.route([OUT_OF_SCOPE, "counts"], "how many reports, and tell me a joke")
    assert d["reply_kind"] == "medical_refusal" and d["run_queries"] == ["counts"]


@pytest.mark.parametrize("q", [
    "what can you do", "Hi, what can you do?", "HELP", "help!", "who are you",
    "how do I use this tool please", "What can I ask you?", "how does this work"])
def test_help_requests_get_the_help_reply_and_no_query(q):
    d = gr.route(["problems", OUT_OF_SCOPE], q)
    assert d["reply_kind"] == "help" and d["reply"] == gr.HELP_REPLY
    assert d["run_queries"] == [] and not d["out_of_scope"] and d["refusal"] is None


@pytest.mark.parametrize("q", [
    "help, my pump is beeping what should i do", "help me decide between two pumps",
    "what can you do about my pump alarm", "can you help me with my insulin dose"])
def test_help_needs_the_whole_question(q):
    assert not gr.is_help(q)


def test_an_advice_rule_always_beats_help(monkeypatch):
    # No real help phrase triggers an advice rule; force the overlap to test the guard.
    monkeypatch.setattr(gr, "is_help", lambda q: True)
    d = gr.route([], "should I stop using my pump")
    assert d["reply_kind"] == "medical_refusal" and d["out_of_scope"]


def test_no_advice_phrasing_is_taken_for_help():
    advice = [t for _, t in TEMPLATES[OUT_OF_SCOPE] if t not in UNRELATED] + ADVICE
    assert not [q for q in advice if gr.is_help(q)]


@pytest.mark.parametrize("q", FACTUAL)
def test_factual_questions_get_no_fixed_reply(q):
    d = gr.route(["counts"], q)
    assert d["reply_kind"] is None and d["reply"] is None and d["run_queries"] == ["counts"]


# ── generator ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def data():
    return gen.generate(n_train=900, n_val=300, seed=7)


def test_generation_is_deterministic():
    a = gen.generate(50, 20, seed=3)
    b = gen.generate(50, 20, seed=3)
    assert a == b


def test_name_pools_are_disjoint():
    pools = gen.split_pools(gen.load_slots(), seed=7)
    for key in gen.SPLIT_SLOTS:
        assert not set(pools["train"][key]) & set(pools["val"][key]), key
        assert pools["val"][key] and pools["train"][key]


def test_validation_never_names_a_training_device(data):
    pools = gen.split_pools(gen.load_slots(), seed=7)
    train_only = [d.lower() for d in pools["train"]["device"]]
    for row in data["val"]:
        text = row["text"].lower()
        assert not any(d in text for d in train_only), row["text"]


def test_templates_are_held_out_per_label():
    split = gen.split_templates(TEMPLATES, seed=7)
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
    clf = QueryRouter.load(model_dir, policy="strict")
    for q in ADVICE:
        out = clf.classify(q)
        assert out["out_of_scope"] and out["run_queries"] == [], q
        assert out["refusal"] == gr.REFUSAL


def test_classify_answers_help_without_queries(model_dir):
    out = QueryRouter.load(model_dir).classify("what can you do?")
    assert out["reply_kind"] == "help" and out["run_queries"] == [] and out["unclear"] is False


def test_classify_routes_factual_questions(model_dir):
    clf = QueryRouter.load(model_dir)
    out = clf.classify("How many death reports does the t:slim X2 have since 2020?")
    assert "counts" in out["run_queries"]     # runs even if the model also flags it
    assert set(out["scores"]) == set(LABELS) and out["unclear"] is False


def test_load_rejects_label_mismatch(model_dir, tmp_path):
    meta = json.loads((model_dir / "meta.json").read_text())
    meta["labels"] = ["counts"]
    (tmp_path / "meta.json").write_text(json.dumps(meta))
    (tmp_path / "model.joblib").write_bytes((model_dir / "model.joblib").read_bytes())
    with pytest.raises(ValueError):
        QueryRouter.load(tmp_path)


def test_evaluate_reports_safety_numbers(model_dir):
    rows = ([{"text": q, "labels": [OUT_OF_SCOPE], "audience": "patient", "oos_kind": "advice"}
             for q in ADVICE]
            + [{"text": "what is the capital of peru", "labels": [OUT_OF_SCOPE],
                "audience": "neutral", "oos_kind": "unrelated"}]
            + [{"text": "How many death reports does the t:slim X2 have since 2020?",
                "labels": ["counts"], "audience": "researcher"}])
    r = evaluate(QueryRouter.load(model_dir), rows)
    assert r["advice_recall"] == 1.0 and r["missed_advice"] == []
    assert sum(r["unrelated"].values()) == 1
    assert set(r["slices"]) == {"patient", "neutral", "researcher"}
    assert r["rule_false_alarms"]["of_in_scope"] == 1


# ── narrowed rules, three-way split, frozen rules, false alarms ──────────────

TRIGGER_WORD_FACTUAL = [
    "Were dangerous malfunctions reported for the Alaris infusion pump?",
    "What is the best way to report a problem with my pump?",
    "how many units were recalled in the DreamStation recall",
    "can i get the recall notice for the Omnipod 5",
    "what symptoms do patients report with hip implants",
    "how many reports mention replacing my battery type",
    "were there dosing errors reported for infusion pumps",
    "Should I search by product code or brand?",
]


@pytest.mark.parametrize("q", TRIGGER_WORD_FACTUAL)
def test_rules_need_a_request_to_judge_or_advise_not_a_word(q):
    assert gr.rule_hits(q) == [], q


@pytest.mark.parametrize("q, rule", [
    ("how many units should i take", "dosing"),
    ("my dad has swelling near his implant", "symptoms"),
    ("is the da Vinci Xi safe", "safety_judgement"),
    ("i want to switch pumps", "stop_switch"),
    ("is the t:slim better than the omnipod", "comparison"),
])
def test_narrowed_rules_still_catch_requests(q, rule):
    assert rule in gr.rule_hits(q), q


def test_default_policy_answers_facts_and_refuses_advice():
    d = gr.route(["counts"], "How many deaths has X had? Should I stop using it?")
    assert gr.DEFAULT_POLICY == "facts_with_refusal"
    assert d["run_queries"] == ["counts"] and d["refusal"] == gr.REFUSAL


def test_heldout_split_is_disjoint_from_train_and_val():
    pools = gen.split_pools(gen.load_slots(), seed=7)
    split = gen.split_templates(TEMPLATES, seed=7)
    for key in gen.SPLIT_SLOTS:
        assert not set(pools["heldout"][key]) & (set(pools["train"][key]) | set(pools["val"][key]))
    for label in LABELS:
        ho = set(split["heldout"][label])
        assert ho and not ho & (set(split["train"][label]) | set(split["val"][label])), label
        if label == OUT_OF_SCOPE:
            assert {t in UNRELATED for _, t in ho} == {True, False}


def test_enough_advice_phrasings():
    assert sum(t not in UNRELATED for _, t in TEMPLATES[OUT_OF_SCOPE]) >= 50


def test_threshold_tuned_on_advice_recall_not_unrelated():
    probs, y = _toy()
    advice = np.zeros(len(y), bool)
    advice[: len(y) // 4] = True
    y[:, OOS] = 0
    y[: len(y) // 2, OOS] = 1                                     # half advice, half unrelated
    unrelated = (y[:, OOS] == 1) & ~advice
    probs[:, OOS] = np.where(advice, 0.9, np.where(unrelated, 0.1, 0.01))
    assert tune_thresholds(probs, y)[OUT_OF_SCOPE] <= 0.1         # unrelated drag it down
    assert tune_thresholds(probs, y, advice_mask=advice)[OUT_OF_SCOPE] == OOS_MAX_THRESHOLD


def test_rules_fingerprint_recorded(model_dir):
    meta = json.loads((model_dir / "meta.json").read_text())
    assert meta["rules_fingerprint"] == gr.rules_fingerprint() and len(meta["rules_fingerprint"]) == 12


def test_evaluate_reports_three_views_and_false_alarms(model_dir):
    clf = QueryRouter.load(model_dir)
    rows = [{"text": q, "labels": [OUT_OF_SCOPE], "oos_kind": "advice"} for q in ADVICE]
    r = evaluate(clf, rows + [{"text": FACTUAL[0], "labels": ["counts"]}])
    assert set(r["safety"]) == {"rules", "model", "both"}
    assert r["safety"]["rules"]["advice_recall"] == 1.0 and r["safety"]["both"]["advice_recall"] == 1.0
    fa = evaluate_false_alarms(clf, [{"text": q, "labels": ["counts"]} for q in TRIGGER_WORD_FACTUAL])
    assert fa["flagged_rate"]["rules"] == 0.0 and set(fa["flagged_rate"]) == {"rules", "model", "both"}


def test_false_alarm_set_file_is_valid():
    path = Path(__file__).resolve().parents[1] / "data/test/false_alarm_set.jsonl"
    rows = gen.read_jsonl(path)
    assert len(rows) >= 40
    assert all(OUT_OF_SCOPE not in r["labels"] and r["trigger"] and to_vector(r["labels"]) for r in rows)
