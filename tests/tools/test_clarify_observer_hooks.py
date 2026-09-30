"""Observer hook coverage for clarify tool requests/responses."""

import json


from tools.clarify_tool import clarify_tool


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


def test_clarify_fires_pre_and_post_hooks_per_question_without_answer_text(monkeypatch):
    recorder = HookRecorder()
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    def callback(questions, *, request_id=None):
        assert request_id == "batch-req"
        assert [q["qid"] for q in questions] == ["q0", "q1"]
        return {"answers": {"q0": "blue", "q1": ["cat"]}, "outcome": "submitted"}

    result = json.loads(clarify_tool(
        [
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
        {"session_id": "sess-batch", "request_id": "batch-req", "question": "Color?",
         "choices": ["blue", "red"], "multi_select": False, "platform": "tui"},
        {"session_id": "sess-batch", "request_id": "batch-req", "question": "Animal?",
         "choices": ["cat", "dog"], "multi_select": True, "platform": "tui"},
    ]
    assert recorder.by_name("post_clarify_response") == [
        {"session_id": "sess-batch", "request_id": "batch-req", "outcome": "answered", "platform": "tui"},
        {"session_id": "sess-batch", "request_id": "batch-req", "outcome": "answered", "platform": "tui"},
    ]
    assert all("user_response" not in call and "answer" not in call for call in recorder.by_name("post_clarify_response"))


def test_legacy_callback_without_request_id_still_gets_hooks(monkeypatch):
    recorder = HookRecorder()
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    result = json.loads(clarify_tool(
        [{"question": "Proceed?", "choices": ["yes", "no"]}],
        callback=lambda questions: {"answers": {}, "outcome": "cancelled"},
        session_id="sess-1",
        platform="cli",
    ))

    assert result["outcome"] == "cancelled"
    pre = recorder.by_name("pre_clarify_request")
    post = recorder.by_name("post_clarify_response")
    assert len(pre[0]["request_id"]) == 32  # hook-only uuid4 hex off the serve bridge
    assert [p["outcome"] for p in post] == ["cancelled"]
    assert post[0]["request_id"] == pre[0]["request_id"]


def test_raising_clarify_hook_does_not_break_tool(monkeypatch):
    recorder = HookRecorder(raise_on="pre_clarify_request")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", recorder)

    result = json.loads(clarify_tool(
        [{"question": "Proceed?", "choices": ["yes", "no"]}],
        callback=lambda questions, **kwargs: {"answers": {"q0": "yes"}, "outcome": "submitted"},
        session_id="sess-raise",
        request_id="req-raise",
        platform="cli",
    ))

    assert result["responses"][0]["user_response"] == "yes"
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

    def callback(questions, *, request_id=None):
        return server._clarify_block("sid-1", questions, request_id=request_id)

    result = json.loads(clarify_tool(
        [{"question": "Color?", "choices": ["green", "purple"]}],
        callback=callback,
        session_id="durable-session",
        request_id="srq-test123",
        platform="serve",
    ))
    assert frames[0]["id"] == "srq-test123"
    assert frames[0]["method"] == "clarify"
    assert cancellations == [("request.cancel", "sid-1", {"id": "srq-test123", "method": "clarify", "reason": "timeout"})]
    assert [kwargs["outcome"] for name, kwargs in seen if name == "post_clarify_response"] == ["timeout"]
    assert result["outcome"] == "timed_out"


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

    def callback(questions, *, request_id=None):
        return server._clarify_block("sid-2", questions, request_id=request_id)

    clarify_tool([{"question": "Shape?", "choices": ["circle", "square"]}], callback=callback,
                 session_id="durable-session", platform="serve")

    pre_request_id = [kwargs["request_id"] for name, kwargs in seen if name == "pre_clarify_request"][0]
    post_request_id = [kwargs["request_id"] for name, kwargs in seen if name == "post_clarify_response"][0]
    assert pre_request_id == post_request_id == frames[0]["id"]
    assert frames[0]["id"].startswith("srq-")
