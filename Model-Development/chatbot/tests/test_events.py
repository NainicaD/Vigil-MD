"""Tests for api/events.py: the monitoring event (pure function, no network)."""
import json
from datetime import datetime, timezone

from api import events


def answer_out(**over):
    out = {"status": "answered", "labels": ["counts", "recalls"], "reply_kind": None,
           "rule_hits": [], "rules_fingerprint": "r", "template_fingerprint": "t",
           "entities": [{"type": "device", "id": "dev:1", "found": True},
                        {"type": "manufacturer", "id": "m:2", "found": False}],
           "budget_gb": {"max": 0.5, "spent": 0.0123},
           "search": {"phrase": "secret phrase", "matches": []},
           "text": "secret answer",
           "router_signals": {"scores": {"counts": 0.9, "recalls": 0.6, "out_of_scope": 0.01},
                              "unclear": False, "unknown_word_share": 0.25}}
    out.update(over)
    return out


def build(out, question="How many deaths, and was it recalled?", **kw):
    args = dict(latency_ms=12.345, model_version="v", dataset="d", entity_given=False,
                request_id="abc", now=datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc))
    args.update(kw)
    return events.build_event(out, question, **args)


def test_event_fields():
    e = build(answer_out())
    assert e["ts"] == "2026-11-19T12:00:00.000+00:00" and e["request_id"] == "abc"
    assert e["labels"] == ["counts", "recalls"] and e["top_score"] == 0.9
    assert e["unknown_word_share"] == 0.25 and e["unclear"] is False
    assert e["entity_types_found"] == ["device"] and e["n_entities"] == 1   # found only
    assert e["budget_gb_spent"] == 0.0123 and e["latency_ms"] == 12.3
    assert e["question_chars"] == 37 and e["question_words"] == 7
    json.dumps(e)                                     # JSON-serialisable


def test_event_has_no_text_ids_or_search_phrase():
    text = json.dumps(build(answer_out(), question="my secret question about dev:1"))
    for part in ("secret", "dev:1", "m:2", "question about"):
        assert part not in text, part


def test_event_records_which_context_rule_was_used_and_nothing_else():
    assert build(answer_out())["context_used"] is None
    assert build(answer_out(context_used="entity"))["context_used"] == "entity"
    assert build(answer_out(context_used="previous_question"))["context_used"] == "previous_question"
    # Only the known values: free text placed there by mistake never reaches the event.
    assert build(answer_out(context_used="my secret previous question"))["context_used"] is None


def test_event_has_no_previous_question_text():
    out = answer_out(context_used="previous_question", question="secret previous question")
    text = json.dumps(build(out, question="DreamStation"))
    assert "secret" not in text and "previous question" not in text


def test_event_without_an_answer():
    e = build(None, status="exception")
    assert e["status"] == "exception" and e["labels"] == [] and e["scores"] == {}
    assert e["top_score"] is None and e["n_entities"] == 0 and e["budget_gb_spent"] is None


def test_stdout_sink_writes_one_cloud_logging_json_line(capsys):
    events.stdout_sink({"event": "router_decision", "status": "help"})
    line = capsys.readouterr().out
    assert line.endswith("\n") and line.count("\n") == 1
    data = json.loads(line)
    assert data["severity"] == "INFO" and data["message"] == "router_decision"
    assert data["status"] == "help"


def test_emit_never_raises(caplog):
    def broken(event):
        raise ValueError("no")
    events.emit(broken, out=answer_out(), question="q", latency_ms=1, model_version=None,
                dataset="d", entity_given=False)
    assert "could not write the monitoring event" in caplog.text
