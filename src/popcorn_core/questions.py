"""An agent's question card, and the answers a member posts back to it.

The channel agent's `ask_user` tool puts a ``question`` part on its reply:
one to four questions, each with two to four options. A member answers by
posting a message of their own that carries one ``answers`` part — one answer
per question, by position, each a pick (one, or several under
``multi_select``), free text, both, or a skip. The web and iOS apps build that
part identically, and this module builds it the same way, so the server cannot
tell a CLI answer from a tap.

Two things the apps deliberately leave out, and so does this:

* **An option's ``plan_id``.** A dry run's plan rides on its option, hidden: no
  card shows it and no member reads it. The server copies it into the answers'
  ``metadata.answer_plan_ids`` when the answers land, so the run they start
  finds the dry run they confirm. It does the same for
  ``metadata.answer_descriptions``, and it strips anything a client sent under
  either key — so the part goes up with ``metadata: {}``, as the apps send it,
  and the plan is confirmed by picking its option's label. `card_view` copies
  an option's label and description and nothing else, so neither the id nor a
  hidden field added later can reach output.
* **A question left unanswered.** The server refuses an answers part that does
  not answer every question; the apps send a question the member never got to
  as skipped, and so does `build_answers`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import ERROR_CODE_CONFLICT, ERROR_CODE_NOT_FOUND, PopcornError


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def message_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    return [p for p in parts or [] if isinstance(p, dict)]


def question_part(message: dict[str, Any]) -> dict[str, Any] | None:
    """The message's question part (an agent reply carries at most one)."""
    for part in message_parts(message):
        if part.get("type") == "question":
            return part
    return None


def is_agent_message(message: dict[str, Any]) -> bool:
    """Whether the apps treat `message` as an agent's reply.

    The same three signals the web app reads. The server holds the real rule —
    only the channel agent's own question can be answered — and refuses the
    rest; this is what lets the CLI say so before sending anything.
    """
    metadata = _dict(message.get("metadata"))
    author = _dict(message.get("author"))
    return (
        metadata.get("message_type") == "assistant"
        or metadata.get("is_bot") is True
        or author.get("is_bot") is True
    )


def is_pending(part: dict[str, Any]) -> bool:
    return (part.get("status") or "pending") == "pending" and bool(part.get("questions"))


def card_view(message: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    """The card as a member sees it, numbered the way `message answer` takes it.

    Built from an allowlist rather than by deleting ``plan_id``: whatever else
    the server hides on an option is left behind the same way.
    """
    questions = []
    for qi, q in enumerate(part.get("questions") or [], start=1):
        questions.append(
            {
                "index": qi,
                "header": q.get("header", ""),
                "question": q.get("question", ""),
                "multi_select": bool(q.get("multi_select")),
                "options": [
                    {
                        "index": oi,
                        "label": o.get("label", ""),
                        "description": o.get("description") or "",
                    }
                    for oi, o in enumerate(q.get("options") or [], start=1)
                ],
            }
        )
    author = _dict(message.get("author"))
    return {
        "message_id": message.get("id"),
        "question_id": part.get("question_id"),
        "thread_id": message.get("thread_id"),
        "created_at": message.get("created_at"),
        "author": author.get("display_name") or author.get("username"),
        "status": part.get("status") or "pending",
        "questions": questions,
    }


def pending_cards(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every unanswered agent question in `messages`, newest first.

    History arrives oldest-first. ``latest`` marks the card on the newest
    message, which is the one the apps open on the composer; an older pending
    card is still answerable, and the server takes it.
    """
    cards = []
    newest_id = messages[-1].get("id") if messages else None
    for message in reversed(messages):
        if message.get("deleted_at") or not is_agent_message(message):
            continue
        part = question_part(message)
        if part is None or not is_pending(part):
            continue
        card = card_view(message, part)
        card["latest"] = message.get("id") == newest_id
        cards.append(card)
    return cards


def check_answerable(message: dict[str, Any] | None, message_id: str) -> dict[str, Any]:
    """The question part `message_id` carries, or why it cannot be answered."""
    if message is None:
        raise PopcornError(
            f"Message {message_id} is not among the channel's recent messages",
            error_code=ERROR_CODE_NOT_FOUND,
            hint="a question asked in a thread needs --thread <root message id>",
        )
    part = question_part(message)
    if message.get("deleted_at") or part is None or not is_agent_message(message):
        raise PopcornError(f"Message {message_id} is not an agent's question")
    if not is_pending(part):
        answered_in = part.get("answers_message_id")
        suffix = f" (by message {answered_in})" if answered_in else ""
        raise PopcornError(
            f"That question has already been answered{suffix}",
            error_code=ERROR_CODE_CONFLICT,
        )
    return part


@dataclass
class Selection:
    """What to answer one question with: picks by number or label, free text,
    or a skip. Empty means skipped, as in the apps."""

    options: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    custom: str | None = None
    skip: bool = False


def option_number(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise PopcornError(f"{what} must be a number, got {value!r}")
    try:
        return int(value)
    except ValueError:
        raise PopcornError(f"{what} must be a number, got {value!r}") from None


def selections_from_json(spec: Any) -> dict[int, Selection]:
    """``--answers``: one object per question, in order —
    ``{"option": 1 | [1, 3], "label": "…" | ["…"], "custom": "…", "skip": true}``.
    A question past the end of the list is skipped."""
    if not isinstance(spec, list) or not spec:
        raise PopcornError("--answers must be a JSON list with one object per question")
    out: dict[int, Selection] = {}
    for qi, entry in enumerate(spec, start=1):
        if not isinstance(entry, dict):
            raise PopcornError(f"--answers item {qi} must be an object")
        unknown = sorted(set(entry) - {"option", "label", "custom", "skip"})
        if unknown:
            raise PopcornError(
                f"--answers item {qi} has unknown key {unknown[0]!r} "
                "(expected option, label, custom, skip)"
            )
        options = entry.get("option", [])
        labels = entry.get("label", [])
        custom = entry.get("custom")
        if custom is not None and not isinstance(custom, str):
            raise PopcornError(f"--answers item {qi}: custom must be a string")
        out[qi] = Selection(
            options=[
                option_number(o, f"--answers item {qi} option")
                for o in (options if isinstance(options, list) else [options])
            ],
            labels=[str(label) for label in (labels if isinstance(labels, list) else [labels])],
            custom=custom,
            skip=entry.get("skip") is True,
        )
    return out


def _resolve_label(label: str, offered: list[str], qi: int) -> str:
    """`label` as the card spells it: exact, else the one case-insensitive
    match. The server compares exactly, so the card's spelling is what goes."""
    if label in offered:
        return label
    folded = [o for o in offered if o.casefold() == label.casefold()]
    if len(folded) == 1:
        return folded[0]
    raise PopcornError(
        f"Question {qi} has no option {label!r} (options: {', '.join(offered)})",
    )


def _answer(qi: int, question: dict[str, Any], sel: Selection) -> dict[str, Any]:
    offered = [str(o.get("label", "")) for o in question.get("options") or []]
    selected: list[str] = []
    for n in sel.options:
        if not 1 <= n <= len(offered):
            raise PopcornError(f"Question {qi} has no option {n} (it has 1 to {len(offered)})")
        selected.append(offered[n - 1])
    selected += [_resolve_label(label, offered, qi) for label in sel.labels]
    if len(set(selected)) != len(selected):
        raise PopcornError(f"Question {qi}: the same option is picked twice")
    if len(selected) > 1 and not question.get("multi_select"):
        raise PopcornError(f"Question {qi} takes one option, not {len(selected)}")
    custom = (sel.custom or "").strip()
    if sel.skip and (selected or custom):
        raise PopcornError(f"Question {qi} cannot be skipped and answered at once")
    return {
        "question": question.get("question", ""),
        "selected": selected,
        "custom": custom or None,
        "skipped": not (selected or custom),
    }


def build_answers(part: dict[str, Any], selections: dict[int, Selection]) -> list[dict[str, Any]]:
    """One answer per question, in order: the apps' ``buildAnswersPart``.

    A question with no selection goes as skipped, which is what the apps send
    for a question the member left; a selection for a question the card does
    not have is refused rather than dropped.
    """
    questions = part.get("questions") or []
    stray = sorted(qi for qi in selections if not 1 <= qi <= len(questions))
    if stray:
        raise PopcornError(
            f"The card has no question {stray[0]} (it has {len(questions)})",
        )
    return [
        _answer(qi, q, selections.get(qi, Selection())) for qi, q in enumerate(questions, start=1)
    ]


def answers_part(
    part: dict[str, Any], question_message_id: str, answers: list[dict[str, Any]]
) -> dict[str, Any]:
    """The answers part exactly as the apps put it on the wire.

    ``metadata`` is empty on purpose: the server writes the plan ids and
    descriptions there itself, from the question, and drops a client's.
    """
    now = datetime.now(timezone.utc)
    return {
        "id": str(uuid.uuid4()),
        "type": "answers",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "streaming_state": None,
        "visibility": "public",
        "version": 1,
        "replaced_by": None,
        "replaces": None,
        "error": None,
        "attachments": [],
        "metadata": {},
        "question_id": part.get("question_id"),
        "question_message_id": question_message_id,
        "answers": answers,
    }


def answer_lines(answers: list[dict[str, Any]]) -> list[str]:
    """What will be answered, one line per question, for the confirmation."""
    lines = []
    for i, a in enumerate(answers, start=1):
        bits = [f'"{label}"' for label in a["selected"]]
        if a["custom"]:
            bits.append(f"free text: {a['custom']!r}")
        value = " + ".join(bits) if bits else "(skipped)"
        lines.append(f"  {i}. {a['question']}\n     → {value}")
    return lines
