"""Clarify tool: structured multiple-choice / open-ended questions to the user.
Schema, validation and a thin dispatcher; the UI lives in a platform-provided
callback (cli.py, gateway/run.py, tui_gateway)."""

import inspect
import json
import logging
import uuid
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_CHOICES = 4  # the UI always appends an "Other (type your answer)" row
MAX_QUESTIONS = 5  # independent questions per call
# Per-choice cap. Surfaces wrap long choice text (newlines kept), so anything longer is rejected here, at the
# source, instead of a surface silently dropping a choice that ``choices_offered`` still reports as shown.
MAX_CHOICE_CHARS = 8000
# Applied to the first choice here (not per-surface) so every adapter renders it identically.
RECOMMENDED_LABEL = "(Recommended)"
_UNAVAILABLE = "Clarify tool is not available in this execution context."
# Notice for ``outcome: no_answerer``: the session has no surface anyone could answer on, so the tool returns at
# once instead of blocking for ``clarify_timeout``.
NO_ANSWERER_NOTICE = "No one can answer here; state your question in your reply and stop."
_SHAPE = "Pass questions=[{question, choices?, multi_select?}]; a single question is a one-entry array."


def mark_recommended(choices: List[str]) -> List[str]:
    """Suffix the first choice (schema says best-first) with RECOMMENDED_LABEL; idempotent,
    and a lone choice is left untouched (nothing to prefer it over)."""
    first = str(choices[0]).strip() if choices else ""
    if len(choices) < 2 or first != strip_recommended(first):
        return choices
    return [f"{first} {RECOMMENDED_LABEL}"] + list(choices[1:])


def strip_recommended(text: str) -> str:
    """Remove the recommendation label so presentation never leaks into ``user_response``."""
    stripped = str(text).strip()
    if stripped.casefold().endswith(RECOMMENDED_LABEL.casefold()):
        return stripped[: -len(RECOMMENDED_LABEL)].strip()
    return stripped


def _accepts_kwarg(callback, name: str) -> bool:
    """Signature-inspect (never a TypeError retry, which could re-prompt the user) whether
    ``callback`` takes ``name`` or ``**kwargs``; non-introspectable callables are legacy."""
    try:
        params = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
def _clean_answer(raw, multi: bool):
    """Strip presentation (the label, multi-select JSON) from a locked answer: a multi-select
    answer is a list, a JSON array string, or one typed ("Other") answer."""
    if not multi:
        return strip_recommended(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        raw = parsed if isinstance(parsed, list) else [raw]
    return [strip_recommended(item) for item in raw if str(item).strip()]


def _clarify_request_id(request_id: Optional[str] = None, *, server_request: bool = False) -> str:
    supplied = str(request_id or "").strip()
    if supplied:
        return supplied
    if server_request:
        return f"srq-{uuid.uuid4().hex[:12]}"
    return uuid.uuid4().hex


def _uses_server_request_id(callback, platform: str) -> bool:
    """The TUI/Desktop serve bridge uses this id as the JSON-RPC server-request frame id.

    Other surfaces keep the hook-only uuid4 hex id promised by the hook contract.
    """
    return _accepts_kwarg(callback, "request_id") and (platform or "").lower() in {"serve", "desktop", "tui"}


def _invoke_clarify_hook(hook_name: str, **kwargs) -> None:
    """Best-effort clarify observer dispatch; observer failures never affect the tool."""
    try:
        from hermes_cli.lifecycle import invoke_hook
        invoke_hook(hook_name, **kwargs)
    except Exception as exc:
        logger.debug("Clarify hook %s dispatch failed: %s", hook_name, exc)


def _emit_pre_clarify(entry: dict, *, session_id: str, request_id: str, platform: str) -> None:
    _invoke_clarify_hook(
        "pre_clarify_request",
        session_id=session_id or "",
        request_id=request_id,
        question=entry["question"],
        choices=entry["choices_offered"],
        multi_select=bool(entry["multi_select"]),
        platform=platform or "",
    )


def _emit_post_clarify(entries: List[dict], *, session_id: str, request_id: str, platform: str, outcome: str) -> None:
    for _entry in entries:
        _invoke_clarify_hook(
            "post_clarify_response",
            session_id=session_id or "",
            request_id=request_id,
            outcome=outcome,
            platform=platform or "",
        )


# Callback reply ``outcome`` -> the observer-hook vocabulary (stable for existing hook consumers).
_HOOK_OUTCOMES = {"submitted": "answered", "timed_out": "timeout", "cancelled": "cancelled",
                  "undelivered": "undelivered", "no_answerer": "no_answerer"}


def _clarify_hook_outcome(reply) -> str:
    outcome = reply.get("outcome") if isinstance(reply, dict) else None
    return _HOOK_OUTCOMES.get(str(outcome or ""), "answered")
def _normalize_questions(questions) -> tuple:
    """Validate ``questions`` -> ``(normalized, error)``. Entries carry ``qid`` (stable wire id
    ``q<index>`` surfaces key answers by), ``question``, decorated ``choices``, bare
    ``choices_offered`` and ``multi_select``."""
    if not isinstance(questions, list) or not questions:
        return None, f"questions must be a non-empty array. {_SHAPE}"
    if len(questions) > MAX_QUESTIONS:
        return None, f"questions supports at most {MAX_QUESTIONS} items."
    normalized = []
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            return None, f"questions[{index}] must be an object. {_SHAPE}"
        text = str(item.get("question") or "").strip()
        if not text:
            return None, f"questions[{index}].question must be non-empty text."
        choices = item.get("choices")
        if choices is not None:
            if not isinstance(choices, list) or not all(isinstance(c, str) for c in choices):
                return None, f"questions[{index}].choices must be a list of strings."
            for pos, choice in enumerate(choices):
                if len(choice.strip()) > MAX_CHOICE_CHARS:
                    return None, (f"questions[{index}].choices[{pos}] is {len(choice.strip())} characters; the limit is "
                                  f"{MAX_CHOICE_CHARS}. Move detail into the question text and resend.")
            cleaned = [c.strip() for c in choices if c.strip()][:MAX_CHOICES]
            if choices and not cleaned:
                # A card with no options strands the turn: say so instead of silently asking open-ended.
                return None, (f"questions[{index}].choices has {len(choices)} entries but all are blank. "
                              "Send real labels, or omit choices to ask open-ended.")
            choices = cleaned or None
        normalized.append({
            "qid": f"q{index}", "question": text,
            "choices": mark_recommended(list(choices)) if choices else None,
            "choices_offered": list(choices) if choices else None,
            "multi_select": bool(item.get("multi_select")) and bool(choices)})
    return normalized, None


def _response_status(qid: str, answers: dict, multi: bool) -> tuple:
    raw = answers.get(qid)
    cleaned = _clean_answer(raw, multi) if raw not in (None, "") else None
    if cleaned:
        return "answered", cleaned
    return ("skipped" if qid in answers else "unanswered"), None


def _result(normalized: List[dict], reply: dict) -> str:
    """Result JSON from a callback reply ``{"answers": {qid: raw | None}, "outcome", "notice"?}``:
    every response carries ``status`` and ``user_response`` (null unless answered); ``outcome``
    says how the wait ended and ``notice`` (surface-supplied) says why."""
    answers = reply.get("answers") or {}
    responses = []
    for entry in normalized:
        status, value = _response_status(entry["qid"], answers, entry["multi_select"])
        responses.append({"question": entry["question"], "choices_offered": entry["choices_offered"],
                          "status": status, "user_response": value})
    result: Dict[str, object] = {"responses": responses, "outcome": reply["outcome"]}
    if reply.get("notice"):
        result["notice"] = str(reply["notice"])
    return json.dumps(result, ensure_ascii=False)


def clarify_tool(questions, callback: Optional[Callable] = None, *, session_id: str = "",
                 request_id: Optional[str] = None, platform: str = "") -> str:
    """Ask 1-5 questions in one call. ``callback(questions) -> {"answers", "outcome", "notice"?}``
    is platform injected (cli.py / gateway / tui_gateway) and receives the normalized list;
    callbacks that accept ``request_id`` also get the id the clarify observer hooks report."""
    normalized, error = _normalize_questions(questions)
    if error:
        return tool_error(error)
    if callback is None:
        return tool_error(_UNAVAILABLE)
    request_id = _clarify_request_id(request_id, server_request=_uses_server_request_id(callback, platform))
    for entry in normalized:
        _emit_pre_clarify(entry, session_id=session_id, request_id=request_id, platform=platform)
    outcome = "error"
    try:
        if _accepts_kwarg(callback, "request_id"):
            reply = callback(normalized, request_id=request_id)
        else:
            reply = callback(normalized)
        result = _result(normalized, reply)
        outcome = _clarify_hook_outcome(reply)
        return result
    except Exception as exc:
        outcome = "error"
        return tool_error(f"Failed to get user input: {exc}")
    finally:
        _emit_post_clarify(normalized, session_id=session_id, request_id=request_id, platform=platform, outcome=outcome)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user one or more questions when you need a decision, "
        "clarification, or feedback before proceeding. Pass every question "
        f"in `questions` (1-{MAX_QUESTIONS} entries) — a single question is a "
        "one-entry array, and several INDEPENDENT questions belong in ONE "
        "call (one form beats a chain of clarify calls; if one answer would "
        "change another question, ask separately). Per question: "
        f"single-select (up to {MAX_CHOICES} choices — put your recommended "
        "option FIRST, the UI marks it '(Recommended)' and auto-appends an "
        "'Other' free-text row), multi-select (multi_select=true), or "
        "open-ended (omit choices). Options go ONLY in `choices`, never "
        "enumerated inside the question text (choices render as pickable "
        "rows; options written into the question are dead prose the user "
        "can't click). Result: {responses: [...], outcome} in question order. "
        "Each response has status answered, skipped or unanswered "
        "(user_response is null unless answered); outcome is submitted, "
        "cancelled, timed_out, undelivered or no_answerer (nobody can answer here: ask in your reply and stop), "
        "with a notice saying why "
        "when the wait ended without a submit. Prefer deciding "
        "low-stakes questions yourself; don't use this for dangerous-command "
        "confirmation (the terminal tool handles that)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": (
                    "The question(s). Each: question text (options excluded), "
                    "optional choices (recommended first; omit for free-text), "
                    "optional multi_select. Responses come back in question "
                    "order with the question text echoed."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "choices": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": MAX_CHOICE_CHARS},
                            "maxItems": MAX_CHOICES,
                        },
                        "multi_select": {"type": "boolean"},
                    },
                    "required": ["question"],
                },
            },
        },
        "required": ["questions"],
    },
}

# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        args.get("questions"),
        callback=kw.get("callback"),
        session_id=kw.get("session_id") or kw.get("task_id") or "",
        platform=kw.get("platform") or "",
        request_id=kw.get("request_id")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
