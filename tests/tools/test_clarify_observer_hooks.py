"""Observer hook coverage for clarify tool requests/responses."""

import json


from tools.clarify_tool import clarify_tool, TIMEOUT_RESPONSE


class HookRecorder:
    def __init__(self, *, raise_on=None):
        self.raise_on = raise_on
        self.calls = []

    def __call__(self, hook_name, **kwargs):
        self.calls.append((hook_name, kwargs))
        if hook_name == self.raise_on:
            raise RuntimeError("hook boom")
        return []

    def by_name(self, name):
        return [kwargs for hook, kwargs in self.calls if hook == name]


def test_single_clarify_fires_pre_and_post_hooks_without_answer_text(monkeypatch):
    recorder = HookRecorder()
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    def callback(question, choices, *, multi_select=False, request_id=None):
        assert request_id == "req-1"
        return "Paris"

    result = json.loads(clarify_tool(
        "Capital?",
        choices=["Paris", "Berlin"],
        callback=callback,
        session_id="sess-1",
        request_id="req-1",
        platform="serve",
    ))

    assert result["user_response"] == "Paris"
    pre = recorder.by_name("pre_clarify_request")
    post = recorder.by_name("post_clarify_response")
    assert pre == [{
        "session_id": "sess-1",
        "request_id": "req-1",
        "question": "Capital?",
        "choices": ["Paris", "Berlin"],
        "multi_select": False,
        "platform": "serve",
    }]
    assert post == [{
        "session_id": "sess-1",
        "request_id": "req-1",
        "outcome": "answered",
        "platform": "serve",
    }]
    assert "answer" not in post[0]
    assert "user_response" not in post[0]


def test_batch_clarify_fires_hooks_once_per_question(monkeypatch):
    recorder = HookRecorder()
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    def callback(question, choices, *, questions=None, request_id=None):
        assert request_id == "batch-req"
        assert [q["qid"] for q in questions] == ["q0", "q1"]
        return {"answers": {"q0": "blue", "q1": "cat"}}

    result = json.loads(clarify_tool(
        "Two questions",
        questions=[
            {"question": "Color?", "choices": ["blue", "red"]},
            {"question": "Animal?", "choices": ["cat", "dog"], "multi_select": True},
        ],
        callback=callback,
        session_id="sess-batch",
        request_id="batch-req",
        platform="tui",
    ))

    assert [r["user_response"] for r in result["responses"]] == ["blue", ["cat"]]
    assert recorder.by_name("pre_clarify_request") == [
        {
            "session_id": "sess-batch",
            "request_id": "batch-req",
            "question": "Color?",
            "choices": ["blue", "red"],
            "multi_select": False,
            "platform": "tui",
        },
        {
            "session_id": "sess-batch",
            "request_id": "batch-req",
            "question": "Animal?",
            "choices": ["cat", "dog"],
            "multi_select": True,
            "platform": "tui",
        },
    ]
    assert recorder.by_name("post_clarify_response") == [
        {"session_id": "sess-batch", "request_id": "batch-req", "outcome": "answered", "platform": "tui"},
        {"session_id": "sess-batch", "request_id": "batch-req", "outcome": "answered", "platform": "tui"},
    ]
    assert all("user_response" not in call and "answer" not in call for call in recorder.by_name("post_clarify_response"))


def test_raising_clarify_hook_does_not_break_tool(monkeypatch):
    recorder = HookRecorder(raise_on="pre_clarify_request")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    result = json.loads(clarify_tool(
        "Proceed?",
        choices=["yes", "no"],
        callback=lambda question, choices, **kwargs: "yes",
        session_id="sess-raise",
        request_id="req-raise",
        platform="cli",
    ))

    assert result["user_response"] == "yes"
    assert recorder.by_name("pre_clarify_request")
    assert recorder.by_name("post_clarify_response")


def test_serve_clarify_block_uses_hook_request_id_as_ws_event_id(monkeypatch):
    from tui_gateway import server, server_requests

    frames = []
    cancellations = []
    seen = []
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 0)
    server_requests.reset_for_tests()
    monkeypatch.setattr(server_requests, "_write", lambda frame: frames.append(frame))
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: cancellations.append((event, sid, payload)))
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kwargs: seen.append((name, kwargs)))

    def callback(question, choices, *, request_id=None, multi_select=False):
        return server._clarify_block("sid-1", question, choices, multi_select=multi_select, request_id=request_id)

    result = json.loads(clarify_tool(
        "Color?",
        choices=["green", "purple"],
        callback=callback,
        session_id="durable-session",
        request_id="srq-test123",
        platform="serve",
    ))
    assert frames[0]["id"] == "srq-test123"
    assert frames[0]["method"] == "clarify"
    assert cancellations == [("request.cancel", "sid-1", {"id": "srq-test123", "method": "clarify", "reason": "timeout"})]
    assert [kwargs["outcome"] for name, kwargs in seen if name == "post_clarify_response"] == ["timeout"]
    assert result["user_response"] == TIMEOUT_RESPONSE


def test_serve_clarify_mints_server_request_id_for_hook_and_ws_event(monkeypatch):
    from tui_gateway import server, server_requests

    frames = []
    seen = []
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: 0)
    server_requests.reset_for_tests()
    monkeypatch.setattr(server_requests, "_write", lambda frame: frames.append(frame))
    monkeypatch.setattr(server_requests, "_emit", lambda event, sid, payload: None)
    monkeypatch.setattr(server_requests, "_answerable", lambda sid: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kwargs: seen.append((name, kwargs)))

    def callback(question, choices, *, request_id=None, multi_select=False):
        return server._clarify_block("sid-2", question, choices, multi_select=multi_select, request_id=request_id)

    clarify_tool("Shape?", choices=["circle", "square"], callback=callback, session_id="durable-session", platform="serve")

    pre_request_id = [kwargs["request_id"] for name, kwargs in seen if name == "pre_clarify_request"][0]
    post_request_id = [kwargs["request_id"] for name, kwargs in seen if name == "post_clarify_response"][0]
    assert pre_request_id == post_request_id == frames[0]["id"]
    assert frames[0]["id"].startswith("srq-")
