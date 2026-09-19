"""What starts one flow — derived from its bundle, plus the live schedule.

`popcorn flow get` used to answer only "which bundle did this come from",
which is provenance, not the question anyone actually has. The question is
*what makes this run*, and answering it meant checking the whole bundle out
and grepping it by hand.

Four sources answer it, and all but the first are static bundle content:

| Trigger | Read from |
|---|---|
| a schedule | the LIVE scheduled-flow list, never the manifest |
| a webhook, a document upload | `manifest.yaml`'s `webhooks:` / `documents:` |
| a state edge, with or without a button | `manifest.yaml`'s `states.transitions` |
| another flow's step | every bundle flow, for a `foundation.workflow.start_flow` |

**Schedules come from the live list on purpose.** A manifest declares what an
install *creates*; the platform then rewrites installed schedules in place —
`set_app_mode` retunes cadences, the de-peak offset moves a daily cron off the
minute it declares. Answering from the declaration is wrong whenever either has
run, and on a channel read while this was written the manifest said 900 seconds
and the schedule said 180. `schedule_drift` is where declared-vs-live belongs;
here the only useful answer is what is actually armed.

**A dynamic launcher is not a trigger, and not nothing either.** The CTA
engine picks its target at run time (`flow_name: $steps.resolve.output.flow_name`,
computed by a code block from the manifest's state graph), so no static read
can say which flows it reaches. Those steps are reported separately rather than
dropped: silence would let a flow the engine launches every day be reported as
dead, and "nothing runs this" is the one answer here that has to be trustworthy.

Two parsing details that are not obvious and that a reader will otherwise
rediscover the hard way:

* **`on:` is a YAML 1.1 boolean.** A transition's `on: staff.retain` parses to
  the key `True`, not `"on"`, under every safe loader in use. `_edge_event`
  reads both spellings.
* **`agent_runnable_flows` is a list in most manifests and a JSON string in at
  least one.** Scalars are strings on the wire and some bundles write the
  serialized form directly, so both are accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import flow_rules

# The activity a flow uses to launch a sibling flow. One wire name, owned by
# the server's catalog; a bundle that reaches another flow any other way is
# not reachable from here.
START_FLOW_ACTIVITY = "foundation.workflow.start_flow"

# Bundle entries that are never a flow document. Mirrors the reserved set the
# server's own importer applies.
_RESERVED = {
    *flow_rules.MANIFEST_FILENAMES,
    flow_rules.STRINGS_FILENAME,
    flow_rules.AGENT_DOC_FILENAME,
    flow_rules.README_FILENAME,
}

_MANIFEST = flow_rules.MANIFEST_FILENAMES[0]


@dataclass(frozen=True)
class Trigger:
    """One thing that starts this flow.

    `kind` is the stable key an agent branches on: "schedule", "webhook",
    "document", "state" or "flow". `detail` carries the per-kind fields, and
    `summary` is the one human line the CLI prints.
    """

    kind: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "summary": self.summary, **self.detail}


@dataclass
class TriggerReport:
    """Everything known about what starts one flow.

    `triggers` is what definitely starts it. `dynamic_callers` are steps that
    launch *some* flow chosen at run time and may or may not be this one, so
    they are neither claimed as triggers nor hidden. `agent_runnable` is not a
    trigger at all — it is whether `popcorn flow run` is available to the
    channel agent or operator-only — so it stays its own field.
    """

    flow_name: str
    triggers: list[Trigger] = field(default_factory=list)
    dynamic_callers: list[Trigger] = field(default_factory=list)
    agent_runnable: bool = False
    in_bundle: bool = False
    app: str | None = None
    semver: str | None = None

    @property
    def has_trigger(self) -> bool:
        """Whether anything statically names this flow as its target.

        Dynamic launchers deliberately do NOT count. A bundle typically has
        exactly one — the CTA engine — and it can reach any flow, so folding
        it in would make every flow in such a bundle look triggered and the
        "nothing runs this" verdict unsayable, which is the verdict that finds
        dead bundle flows. The caveat is carried by `dynamic_callers` instead,
        for a caller to render beside the verdict rather than inside it.
        """
        return bool(self.triggers)

    def of_kind(self, kind: str) -> list[Trigger]:
        return [t for t in self.triggers if t.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_name": self.flow_name,
            "app": self.app,
            "semver": self.semver,
            "in_bundle": self.in_bundle,
            "has_trigger": self.has_trigger,
            "agent_runnable": self.agent_runnable,
            "triggers": [t.to_dict() for t in self.triggers],
            "dynamic_callers": [t.to_dict() for t in self.dynamic_callers],
        }


def _load(text: str) -> Any:
    """Parse one bundle entry, or None when it is not usable YAML.

    Tolerant by design: this is a read-only explanation of a bundle that is
    already installed and running, so one unparseable entry must cost that
    entry's edges, never the whole answer.
    """
    import yaml

    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def _flow_documents(files: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Every bundle flow, keyed by the name it is installed under.

    Root entries only, matching the tree reader: a YAML in a subdirectory is
    bundle data (a prompt, a block's config), not a flow.
    """
    docs: dict[str, dict[str, Any]] = {}
    for path, text in files.items():
        if "/" in path or path in _RESERVED:
            continue
        if not path.endswith(flow_rules.FLOW_SUFFIXES):
            continue
        doc = _load(text)
        if not isinstance(doc, dict) or "steps" not in doc:
            continue
        name = doc.get("name")
        docs[str(name) if name else path.rsplit(".", 1)[0]] = doc
    return docs


def _walk_steps(steps: Any) -> list[dict[str, Any]]:
    """Every step in a flow, blocks flattened.

    A block (`steps:` inside a step) is where the launches in the real
    bundles live — a sweep and a CTA engine both nest their launches inside
    one — so a top-level-only walk would miss most callers.
    """
    found: list[dict[str, Any]] = []
    if not isinstance(steps, list):
        return found
    for step in steps:
        if not isinstance(step, dict):
            continue
        found.append(step)
        found.extend(_walk_steps(step.get("steps")))
    return found


def _edge_event(edge: dict[Any, Any]) -> str | None:
    """A transition's event name, under either spelling of its key.

    `on:` is a YAML 1.1 boolean, so a safe loader hands back the key `True` —
    which is also why an edge is not a `dict[str, Any]`.
    """
    for key in ("on", True):
        value = edge.get(key)
        if isinstance(value, str):
            return value
    return None


def _as_name_list(value: Any) -> list[str]:
    """`agent_runnable_flows` as a list, from either shape it ships in."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    return []


def _schedule_summary(entry: dict[str, Any]) -> str:
    interval = entry.get("interval_seconds")
    cron = entry.get("cron_expr")
    if interval:
        cadence = f"every {interval}s"
    elif cron:
        cadence = f"cron '{cron}' ({entry.get('timezone') or 'UTC'})"
    else:
        cadence = "no cadence"
    state = "PAUSED" if entry.get("paused") else cadence
    slug = entry.get("slug") or entry.get("schedule_id") or "?"
    tail = (
        f", next {entry['next_run_at']}"
        if entry.get("next_run_at") and not entry.get("paused")
        else ""
    )
    return f"schedule '{slug}' — {state}{tail}"


def _schedule_triggers(flow_name: str, schedules: list[dict[str, Any]]) -> list[Trigger]:
    out = []
    for entry in schedules:
        if not isinstance(entry, dict) or entry.get("flow_id") != flow_name:
            continue
        out.append(
            Trigger(
                "schedule",
                _schedule_summary(entry),
                {
                    "slug": entry.get("slug"),
                    "schedule_id": entry.get("schedule_id"),
                    "interval_seconds": entry.get("interval_seconds"),
                    "cron_expr": entry.get("cron_expr"),
                    "timezone": entry.get("timezone"),
                    "paused": bool(entry.get("paused")),
                    "next_run_at": entry.get("next_run_at"),
                    "note": entry.get("note"),
                },
            )
        )
    return out


def _manifest_triggers(flow_name: str, manifest: dict[str, Any]) -> list[Trigger]:
    """Webhook, document-upload and state edges naming this flow."""
    out: list[Trigger] = []

    for hook in manifest.get("webhooks") or []:
        if isinstance(hook, dict) and hook.get("flow") == flow_name:
            name = hook.get("name") or "?"
            out.append(
                Trigger(
                    "webhook",
                    f"webhook '{name}' delivers to this flow",
                    {"name": hook.get("name"), "description": hook.get("description")},
                )
            )

    for doc in manifest.get("documents") or []:
        if isinstance(doc, dict) and doc.get("flow") == flow_name:
            doc_id = doc.get("id") or "?"
            out.append(
                Trigger(
                    "document",
                    f"uploading the '{doc_id}' channel document runs this flow",
                    {"document": doc.get("id"), "accept": doc.get("accept")},
                )
            )

    out.extend(_state_triggers(flow_name, manifest.get("states")))
    return out


def _state_edges(states: Any) -> list[dict[Any, Any]]:
    """The state graph's edge list.

    `transitions:` is where the shipped graph keeps them; `events:` is read
    too, but only when it holds edges — in the bundle that has both, `events:`
    is a map of event names by tier and carries no `flow:` at all.
    """
    if not isinstance(states, dict):
        return []
    edges: list[dict[Any, Any]] = []
    for key in ("transitions", "events"):
        value = states.get(key)
        if isinstance(value, list):
            edges.extend(e for e in value if isinstance(e, dict))
    return edges


def _state_triggers(flow_name: str, states: Any) -> list[Trigger]:
    """Graph edges that start this flow, and whether a button walks them.

    An edge's `flow:` makes the flow the edge itself; a `then:` entry launches
    it as a consequence of an edge that writes facts. Either way the edge's
    `cta:` is the useful half of the answer — with one, a person clicking a
    button on a row starts this flow; without one, the edge is walked by the
    engine, or by the CTA flow named with an explicit action, and no button
    exists.
    """
    out: list[Trigger] = []
    for edge in _state_edges(states):
        event = _edge_event(edge) or "?"
        cta = edge.get("cta") if isinstance(edge.get("cta"), dict) else None
        label = cta.get("label") if cta else None
        button = f"button '{label}'" if label else "no button"

        if edge.get("flow") == flow_name:
            out.append(
                Trigger(
                    "state",
                    f"state edge {event} IS this flow — {button}",
                    {
                        "event": event,
                        "via": "edge",
                        "cta_label": label,
                        "cta_kind": cta.get("kind") if cta else None,
                        "machine": edge.get("machine"),
                    },
                )
            )

        for consequence in edge.get("then") or []:
            if isinstance(consequence, dict) and consequence.get("flow") == flow_name:
                out.append(
                    Trigger(
                        "state",
                        f"state edge {event} launches this flow — {button}",
                        {
                            "event": event,
                            "via": "then",
                            "cta_label": label,
                            "cta_kind": cta.get("kind") if cta else None,
                            "machine": edge.get("machine"),
                        },
                    )
                )
    return out


def _caller_triggers(
    flow_name: str, docs: dict[str, dict[str, Any]]
) -> tuple[list[Trigger], list[Trigger]]:
    """Sibling flows whose steps launch this one, and those that may.

    A step whose `flow_name` is a reference (`$steps.…`) resolves at run time,
    so it goes in the second list: it cannot be claimed as a trigger and must
    not be dropped.
    """
    named: list[Trigger] = []
    dynamic: list[Trigger] = []

    for caller, doc in sorted(docs.items()):
        for step in _walk_steps(doc.get("steps")):
            if step.get("activity") != START_FLOW_ACTIVITY:
                continue
            args = step.get("args")
            target = args.get("flow_name") if isinstance(args, dict) else None
            if not isinstance(target, str):
                continue
            step_id = step.get("id") or "?"
            each = " (foreach)" if step.get("foreach") is not None else ""
            detail = {"flow": caller, "step": step_id, "foreach": step.get("foreach") is not None}

            if target.startswith("$"):
                dynamic.append(
                    Trigger(
                        "flow",
                        f"{caller} step '{step_id}'{each} launches a flow named at "
                        f"run time ({target})",
                        {**detail, "flow_name_expression": target},
                    )
                )
            elif target == flow_name:
                named.append(
                    Trigger(
                        "flow",
                        f"{caller} step '{step_id}'{each} starts this flow",
                        detail,
                    )
                )
    return named, dynamic


def analyze(
    flow_name: str,
    files: dict[str, str],
    schedules: list[dict[str, Any]] | None = None,
    *,
    app: str | None = None,
    semver: str | None = None,
) -> TriggerReport:
    """What starts `flow_name`, from a bundle tree and the live schedule list.

    `files` maps a bundle path to its text, as `popcorn app checkout` reads it.
    `schedules` is the live scheduled-flow list for the channel; each entry's
    `flow_id` is matched against `flow_name`.

    Offline and total: an absent manifest, an unparseable flow or an empty
    bundle each cost only the edges they would have contributed.
    """
    manifest = _load(files.get(_MANIFEST, "")) or {}
    if not isinstance(manifest, dict):
        manifest = {}
    docs = _flow_documents(files)
    named, dynamic = _caller_triggers(flow_name, docs)

    report = TriggerReport(flow_name=flow_name, app=app, semver=semver)
    report.in_bundle = flow_name in docs
    report.triggers = [
        *_schedule_triggers(flow_name, schedules or []),
        *_manifest_triggers(flow_name, manifest),
        *named,
    ]
    report.dynamic_callers = dynamic
    scalars = manifest.get("scalars")
    if isinstance(scalars, dict):
        report.agent_runnable = flow_name in _as_name_list(scalars.get("agent_runnable_flows"))
    return report
