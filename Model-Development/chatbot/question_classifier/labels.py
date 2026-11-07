"""Question types for the chatbot's "Classify question" step.

Multi-label: one question can need several fixed queries ("How many deaths, and was it
recalled?" → counts + recalls). The chatbot runs the fixed queries for every predicted
label, except when out_of_scope is predicted (see guardrails.py).

Labelling rules (use these when writing test questions, too):
  counts        how many reports: deaths, injuries, malfunctions, totals, trends, comparisons
                of counts between devices or years
  problems      what goes wrong: device problems, patient problems reported
  recalls       recalls, recall reasons, recall class, what users were told
  narratives    what happened in reports: describe, summarize, show example reports
  identity      who makes a device, what a UDI / product code / brand is
  out_of_scope  anything the bot must not answer:
                - medical advice: whether to use, stop, switch, trust a device; dosing;
                  symptoms; what to do about a device problem the user has now
                - safety judgements: "is X safe?", "which is safer/better?" (the bot reports
                  what reports say; it never judges)
                - unrelated topics
                A question can be out_of_scope AND another label ("How many deaths has X had?
                Should I stop using it?"). The routing policy decides what happens then.
"""
from __future__ import annotations

LABELS: tuple[str, ...] = ("counts", "problems", "recalls", "narratives", "identity",
                           "out_of_scope")
OUT_OF_SCOPE = "out_of_scope"
AUDIENCES: tuple[str, ...] = ("patient", "clinician", "researcher", "procurement", "neutral")


def to_vector(labels) -> list[int]:
    """Label set → 0/1 list in LABELS order. Raises on an unknown label."""
    labels = set(labels)
    unknown = labels - set(LABELS)
    if unknown:
        raise ValueError(f"unknown labels: {sorted(unknown)}")
    return [int(lbl in labels) for lbl in LABELS]


def from_vector(vec) -> list[str]:
    return [lbl for lbl, v in zip(LABELS, vec) if v]
