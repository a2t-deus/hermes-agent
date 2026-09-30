"""Queued approval hooks carry the queue ``request_id`` a client answers with.

A notifier plugin (phone push) needs the id ``approval.respond`` resolves by; without it the answer can
never reach the prompt. The pre hook, the post hook, and the queued entry must agree, and a coalesced
follower reports its leader's id.
"""

from tools import approval as mod
from tools import approval_gateway_wait as wait_mod

SESSION_KEY = "approval-hook-request-id"
APPROVAL = {"command": "rm -rf build", "description": "d", "pattern_key": "dangerous", "pattern_keys": ["dangerous"]}


def _hooks(monkeypatch) -> list[tuple[str, dict]]:
    mod._gateway_queues.clear()
    hooks: list[tuple[str, dict]] = []
    monkeypatch.setattr(wait_mod._ctx, "_fire_approval_hook", lambda name, **kw: hooks.append((name, kw)))
    return hooks


def test_hooks_carry_the_id_a_client_resolves_with(monkeypatch):
    hooks = _hooks(monkeypatch)
    sent: list[dict] = []

    def client_answers(event, session_key, *, interrupt_log):
        # The client echoes the id from the request it was sent (approval.respond params.request_id).
        assert mod.resolve_gateway_approval(session_key, "once", request_id=sent[0]["request_id"]) == 1
        return "set"

    monkeypatch.setattr(wait_mod, "_poll_event", client_answers)
    decision = wait_mod._await_gateway_decision(SESSION_KEY, sent.append, APPROVAL)

    assert decision["choice"] == "once"
    (pre_name, pre), (post_name, post) = hooks
    assert (pre_name, post_name) == ("pre_approval_request", "post_approval_response")
    assert pre["request_id"] == post["request_id"] == sent[0]["request_id"]


def test_coalesced_follower_reports_the_leaders_id(monkeypatch):
    hooks = _hooks(monkeypatch)
    leader = wait_mod._ApprovalEntry(APPROVAL)
    mod._gateway_queues[SESSION_KEY] = [leader]

    def leader_answered(event, session_key, *, interrupt_log):
        leader.result = "session"
        return "set"

    monkeypatch.setattr(wait_mod, "_poll_event", leader_answered)
    decision = wait_mod._await_gateway_decision(SESSION_KEY, lambda data: None, APPROVAL)

    assert decision["choice"] == "session" and decision.get("coalesced")
    assert [kw["request_id"] for _, kw in hooks] == [leader.data["request_id"]] * 2
    mod._gateway_queues.clear()
