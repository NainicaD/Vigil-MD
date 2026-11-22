"""One structured event per chatbot question, for model monitoring (Tier 1: no text).

The API writes each event as one JSON line on stdout. Cloud Run sends stdout to Cloud
Logging, which stores a JSON line as structured fields (jsonPayload), so the events can be
filtered there and copied to BigQuery by a log sink. A monitoring job compares them with the
router's test-set baseline (label mix, out_of_scope share, top score, unknown-word share).

What an event never holds: the question text, the search phrase, entity ids, the answer text,
the client's address or any header. Only numbers, labels and versions. The question text is
Tier 2 (a separate, protected store with notice to users), not part of this module.

Fields (SCHEMA_VERSION 1):
  event, schema_version, request_id, ts            "router_decision", 1, uuid4 hex, UTC ISO time
  model_version, rules_fingerprint,                which router / rules / templates answered;
  template_fingerprint, dataset                    model_version is None for a router trained
                                                   at build time (not from Artifact Registry)
  status                                           answer status ("answered", "refused", "help",
                                                   "unclear", "budget", "error", ...) or
                                                   "exception" if answer() raised
  reply_kind, labels, rule_hits                    router decision; rule_hits are pattern names
  scores, top_score, unclear                       router scores per label (6 labels)
  unknown_word_share                               share of words not in the router's vocabulary
  question_chars, question_words                   size of the question, not its content
  entity_given                                     the question came with an entity (device page)
  entity_types_found, n_entities                   e.g. ["device"], 1 (types only, no ids)
  budget_gb_spent                                  BigQuery GB this answer used (None if no query)
  latency_ms                                       time inside answer(), in milliseconds
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Callable

EVENT = "router_decision"
SCHEMA_VERSION = 1

log = logging.getLogger(__name__)


def build_event(out: dict | None, question: str, *, latency_ms: float, model_version: str | None,
                dataset: str, entity_given: bool, status: str | None = None,
                request_id: str | None = None, now: datetime | None = None) -> dict:
    """The event for one answer. Pure function.

    Args:
      out: Chatbot.answer() output, or None if answer() raised.
      question: Only its length and word count are used; the text is never copied.
      status: Overrides out["status"] (e.g. "exception").
    """
    out = out or {}
    signals = out.get("router_signals") or {}
    scores = {k: float(v) for k, v in (signals.get("scores") or {}).items()}
    found = [e for e in out.get("entities") or [] if e.get("found")]
    budget = out.get("budget_gb") or {}
    return {
        "event": EVENT,
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id or uuid.uuid4().hex,
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds"),
        "model_version": model_version,
        "rules_fingerprint": out.get("rules_fingerprint"),
        "template_fingerprint": out.get("template_fingerprint"),
        "dataset": dataset,
        "status": status or out.get("status"),
        "reply_kind": out.get("reply_kind"),
        "labels": list(out.get("labels") or []),
        "rule_hits": list(out.get("rule_hits") or []),
        "scores": scores,
        "top_score": max(scores.values()) if scores else None,
        "unclear": signals.get("unclear"),
        "unknown_word_share": signals.get("unknown_word_share"),
        "question_chars": len(question),
        "question_words": len(question.split()),
        "entity_given": entity_given,
        "entity_types_found": sorted({e.get("type") for e in found if e.get("type")}),
        "n_entities": len(found),
        "budget_gb_spent": budget.get("spent"),
        "latency_ms": round(latency_ms, 1),
    }


def stdout_sink(event: dict) -> None:
    """One JSON line on stdout; "severity" and "message" are Cloud Logging's own fields."""
    sys.stdout.write(json.dumps({"severity": "INFO", "message": EVENT, **event}) + "\n")
    sys.stdout.flush()


def emit(sink: Callable[[dict], None], **kwargs) -> None:
    """Builds and writes one event. Never raises: monitoring must not break an answer."""
    try:
        sink(build_event(**kwargs))
    except Exception:       # pylint: disable=broad-except
        log.exception("could not write the monitoring event")
