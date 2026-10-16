"""Clarify's ``on_human_input_request`` carries the question and its choices (contract C2), never the answer.

A phone-push observer renders answer buttons from these extras; upstream's ``prompt`` alone is redacted and
joins every question. ``answer_request_id`` is the id a client answers the serve with (contract C3).
"""

import json

import pytest

from tools.clarify_tool import clarify_tool

TOKEN = "ghp_" + "Z" * 36


@pytest.fixture
def hooks(monkeypatch):
    calls = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: calls.append((name, kw)))
    return calls


def _payloads(hooks):
    (request,) = [kw for name, kw in hooks if name == "on_human_input_request"]
    (resolved,) = [kw for name, kw in hooks if name == "on_human_input_resolved"]
    return request, resolved


def test_single_question_carries_question_and_offered_choices(hooks):
    reply = clarify_tool([{"question": "Which env?", "choices": ["staging", "prod"]}],
                         callback=lambda qs: {"answers": {"q0": "prod"}, "outcome": "submitted"})
    request, resolved = _payloads(hooks)

    assert request["kind"] == "clarify"
    assert (request["question"], request["choices"], request["multi_select"]) == ("Which env?", ["staging", "prod"], False)
    assert "questions" not in request
    assert resolved["outcome"] == "submitted" and resolved["request_id"] == request["request_id"]
    # The answer reaches the tool result only.
    assert json.loads(reply)["responses"][0]["user_response"] == "prod"
    assert "prod" not in json.dumps([request["prompt"], request["question"], resolved.get("prompt")])


def test_batched_questions_are_listed_and_redacted(hooks):
    clarify_tool([{"question": f"Paste {TOKEN}?"}, {"question": "Pick", "choices": ["a", "b"], "multi_select": True}],
                 callback=lambda qs: {"answers": {}, "outcome": "cancelled"})
    request, resolved = _payloads(hooks)

    assert request["choices"] is None and request["multi_select"] is False
    assert [(q["choices"], q["multi_select"]) for q in request["questions"]] == [(None, False), (["a", "b"], True)]
    assert TOKEN not in json.dumps(request)
    assert resolved["outcome"] == "cancelled"


def test_answer_request_id_is_the_id_the_serve_bridge_answers_with(hooks):
    seen = {}

    def bridge(questions, request_id=None):
        seen["request_id"] = request_id
        return {"answers": {}, "outcome": "timed_out"}

    clarify_tool([{"question": "Proceed?"}], callback=bridge)
    request, _ = _payloads(hooks)

    assert request["answer_request_id"] == seen["request_id"] != request["request_id"]


def test_a_callback_without_request_id_gets_no_answer_request_id(hooks):
    clarify_tool([{"question": "Proceed?"}], callback=lambda qs: {"answers": {}, "outcome": "undelivered"})
    request, _ = _payloads(hooks)

    assert "answer_request_id" not in request
