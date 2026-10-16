"""A queued approval's ``on_human_input_request`` carries the queue id a client answers with.

A notifier plugin (phone push) needs the id ``approval.respond`` resolves by; without it the answer can
never reach the prompt. It rides the human-input hook as ``answer_request_id`` (the pre/post approval
payload stays identical to the classic CLI's), and a coalesced follower opens no second ask.
"""

from tools import approval as mod
from tools import approval_gateway_wait as wait_mod

SESSION_KEY = "approval-hook-request-id"
APPROVAL = {"command": "rm -rf build", "description": "d", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]}


def _hooks(monkeypatch) -> list[tuple[str, dict]]:
    mod._gateway_queues.clear()
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: hooks.append((name, kw)))
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: hooks.append((name, kw)))
    return hooks


def test_human_input_hook_carries_the_id_a_client_resolves_with(monkeypatch):
    hooks = _hooks(monkeypatch)
    sent: list[dict] = []

    def client_answers(event, session_key, *, interrupt_log):
        # The client echoes the id from the request it was sent (approval.respond params.request_id).
        assert mod.resolve_gateway_approval(session_key, "once", request_id=sent[0]["request_id"]) == 1
        return "set"

    monkeypatch.setattr(wait_mod, "_poll_event", client_answers)
    decision = wait_mod._await_gateway_decision(SESSION_KEY, sent.append, APPROVAL)

    assert decision["choice"] == "once"
    (ask,) = [kw for name, kw in hooks if name == "on_human_input_request"]
    assert ask["kind"] == "approval" and ask["answer_request_id"] == sent[0]["request_id"]
    assert all("request_id" not in kw for name, kw in hooks if name.endswith("_approval_request")
               or name.endswith("_approval_response"))


def test_coalesced_follower_opens_no_second_ask(monkeypatch):
    hooks = _hooks(monkeypatch)
    leader = wait_mod._ApprovalEntry(APPROVAL)
    mod._gateway_queues[SESSION_KEY] = [leader]

    def leader_answered(event, session_key, *, interrupt_log):
        leader.result = "session"
        return "set"

    monkeypatch.setattr(wait_mod, "_poll_event", leader_answered)
    decision = wait_mod._await_gateway_decision(SESSION_KEY, lambda data: None, APPROVAL)

    assert decision["choice"] == "session" and decision.get("coalesced")
    assert [name for name, _ in hooks] == ["pre_approval_request", "post_approval_response"]
    mod._gateway_queues.clear()
