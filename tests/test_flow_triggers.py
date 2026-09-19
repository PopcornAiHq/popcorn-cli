"""What starts a flow, derived from a bundle tree plus the live schedules.

The verdict these tests protect is the negative one. "Nothing in this
channel's bundle starts this flow" is what finds a dead bundle flow, and it is
only worth printing if every way a flow can be reached is actually read — so
each trigger kind gets a test that it is found, and the no-trigger case gets a
test that a dynamic launcher does not quietly satisfy it.
"""

from __future__ import annotations

from popcorn_core import flow_triggers
from popcorn_core.flow_triggers import analyze

_MANIFEST = """\
app_type: example-app
version: "1.0.0"
webhooks:
  - name: Intake
    description: Where requests arrive.
    flow: example_intake
documents:
  - id: questions
    flow: example_questions_upsert
    accept: [text]
schedules:
  - flow: example_tick
    slug: example-tick
    interval: 900
scalars:
  agent_runnable_flows:
    - example_tick
    - example_child
states:
  table: tracker
  transitions:
    - on: staff.regenerate
      from: [draft]
      to: regenerating
      flow: example_regenerate
      cta:
        kind: regenerate
        label: Regenerate document
    - on: staff.check
      from: [sent]
      to: [signed]
      machine: document
      flow: example_check
    - on: staff.retain
      from: decidable
      to: needs_decision
      then:
        - flow: example_prepare
      cta:
        kind: retain
        label: Send document
"""

_TICK = """\
name: example_tick
version: 1
steps:
- id: sweep
  activity: foundation.store.list_rows
  args: {table_name: tracker}
- id: children
  foreach: $steps.sweep.output.ids
  as: record_id
  activity: foundation.workflow.start_flow
  args:
    flow_name: example_child
    inputs: {record_id: $record_id}
"""

_CHILD = (
    "name: example_child\nversion: 1\nsteps:\n- id: noop\n  activity: foundation.workflow.now\n"
)

_BUNDLE = {
    "manifest.yaml": _MANIFEST,
    "example_tick.yaml": _TICK,
    "example_child.yaml": _CHILD,
    "example_intake.yaml": "name: example_intake\nsteps: []\n",
    "example_questions_upsert.yaml": "name: example_questions_upsert\nsteps: []\n",
    "example_regenerate.yaml": "name: example_regenerate\nsteps: []\n",
    "example_check.yaml": "name: example_check\nsteps: []\n",
    "example_prepare.yaml": "name: example_prepare\nsteps: []\n",
    "example_orphan.yaml": "name: example_orphan\nsteps: []\n",
}

_LIVE_SCHEDULE = {
    "schedule_id": "channel:00000000-0000-4000-8000-000000000001:flow:example_tick:example-tick",
    "flow_id": "example_tick",
    "slug": "example-tick",
    "interval_seconds": 180,
    "cron_expr": None,
    "timezone": "UTC",
    "paused": False,
    "next_run_at": "2026-09-19T03:46:00+00:00",
    "note": "auto-resumed: set_app_mode",
}


def _kinds(report):
    return [t.kind for t in report.triggers]


class TestSchedules:
    def test_the_live_cadence_is_reported_not_the_declared_one(self):
        """The manifest declares 900s and the channel is armed at 180s.

        Answering from the declaration is the specific failure this whole
        module exists to avoid — the platform retunes installed schedules in
        place, so a manifest's number is routinely several times out.
        """
        report = analyze("example_tick", _BUNDLE, [_LIVE_SCHEDULE])
        schedules = report.of_kind("schedule")
        assert len(schedules) == 1
        assert schedules[0].detail["interval_seconds"] == 180
        assert "180" in schedules[0].summary
        assert "900" not in schedules[0].summary

    def test_a_declared_schedule_with_nothing_live_is_not_a_trigger(self):
        report = analyze("example_tick", _BUNDLE, [])
        assert report.of_kind("schedule") == []

    def test_another_flows_schedule_is_not_borrowed(self):
        report = analyze("example_child", _BUNDLE, [_LIVE_SCHEDULE])
        assert report.of_kind("schedule") == []

    def test_a_cron_schedule_names_its_expression_and_zone(self):
        entry = {
            **_LIVE_SCHEDULE,
            "interval_seconds": None,
            "cron_expr": "3 8 * * *",
            "timezone": "America/New_York",
        }
        summary = analyze("example_tick", _BUNDLE, [entry]).of_kind("schedule")[0].summary
        assert "3 8 * * *" in summary
        assert "America/New_York" in summary

    def test_a_paused_schedule_says_so_instead_of_a_cadence(self):
        entry = {**_LIVE_SCHEDULE, "paused": True}
        trigger = analyze("example_tick", _BUNDLE, [entry]).of_kind("schedule")[0]
        assert "PAUSED" in trigger.summary
        assert trigger.detail["paused"] is True


class TestManifestEdges:
    def test_a_webhook_is_a_trigger(self):
        report = analyze("example_intake", _BUNDLE)
        assert _kinds(report) == ["webhook"]
        assert report.triggers[0].detail["name"] == "Intake"

    def test_a_document_upload_is_a_trigger(self):
        report = analyze("example_questions_upsert", _BUNDLE)
        assert _kinds(report) == ["document"]
        assert report.triggers[0].detail["document"] == "questions"

    def test_a_state_edge_with_a_button_names_the_button(self):
        trigger = analyze("example_regenerate", _BUNDLE).of_kind("state")[0]
        assert trigger.detail["event"] == "staff.regenerate"
        assert trigger.detail["cta_label"] == "Regenerate document"
        assert "Regenerate document" in trigger.summary

    def test_a_state_edge_without_a_button_says_there_is_none(self):
        """The shape a signature-poll edge has: real, but no row renders it.

        Reporting it as a button would send someone looking for one; dropping
        it would hide the only edge the CTA engine can walk to reach the flow.
        """
        trigger = analyze("example_check", _BUNDLE).of_kind("state")[0]
        assert trigger.detail["cta_label"] is None
        assert "no button" in trigger.summary

    def test_a_then_launch_is_attributed_to_the_edge_that_causes_it(self):
        trigger = analyze("example_prepare", _BUNDLE).of_kind("state")[0]
        assert trigger.detail["via"] == "then"
        assert trigger.detail["event"] == "staff.retain"
        assert trigger.detail["cta_label"] == "Send document"

    def test_the_yaml_boolean_key_on_is_read_as_the_event_name(self):
        """`on:` is a YAML 1.1 boolean, so a safe loader keys it `True`.

        Without handling that, every state edge reports its event as "?" — the
        failure is silent and every assertion above still passes on the flow
        name alone.
        """
        assert flow_triggers._edge_event({True: "staff.retain"}) == "staff.retain"
        assert flow_triggers._edge_event({"on": "staff.retain"}) == "staff.retain"
        assert flow_triggers._edge_event({"from": "x"}) is None


class TestCallers:
    def test_a_sibling_step_that_names_the_flow_is_a_trigger(self):
        trigger = analyze("example_child", _BUNDLE).of_kind("flow")[0]
        assert trigger.detail == {"flow": "example_tick", "step": "children", "foreach": True}

    def test_a_launch_inside_a_block_is_found(self):
        """Blocks are where the real bundles keep their launches."""
        bundle = {
            **_BUNDLE,
            "example_tick.yaml": (
                "name: example_tick\nsteps:\n"
                "- id: outer\n"
                "  steps:\n"
                "  - id: inner\n"
                "    activity: foundation.workflow.start_flow\n"
                "    args: {flow_name: example_child}\n"
            ),
        }
        trigger = analyze("example_child", bundle).of_kind("flow")[0]
        assert trigger.detail["step"] == "inner"
        assert trigger.detail["foreach"] is False

    def test_a_run_time_flow_name_is_a_caveat_and_not_a_trigger(self):
        """One dynamic launcher must not make every flow look triggered.

        A bundle's CTA engine picks its target in a code block, so it can
        reach anything. Counting it would make the "nothing runs this" verdict
        unsayable for any bundle that has one — which is every bundle with
        buttons.
        """
        bundle = {
            **_BUNDLE,
            "example_cta.yaml": (
                "name: example_cta\nsteps:\n"
                "- id: launch\n"
                "  foreach: $inputs.record_ids\n"
                "  activity: foundation.workflow.start_flow\n"
                "  args: {flow_name: $steps.resolve.output.flow_name}\n"
            ),
        }
        report = analyze("example_orphan", bundle)
        assert report.triggers == []
        assert report.has_trigger is False
        assert len(report.dynamic_callers) == 1
        caveat = report.dynamic_callers[0]
        assert caveat.detail["flow"] == "example_cta"
        assert caveat.detail["flow_name_expression"] == "$steps.resolve.output.flow_name"

    def test_a_step_that_is_not_start_flow_is_ignored(self):
        bundle = {
            **_BUNDLE,
            "example_other.yaml": (
                "name: example_other\nsteps:\n"
                "- id: post\n"
                "  activity: foundation.channel.post\n"
                "  args: {flow_name: example_child}\n"
            ),
        }
        callers = [t.detail["flow"] for t in analyze("example_child", bundle).of_kind("flow")]
        assert callers == ["example_tick"]


class TestAgentRunnable:
    def test_a_listed_flow_is_agent_runnable(self):
        assert analyze("example_tick", _BUNDLE).agent_runnable is True

    def test_an_unlisted_flow_is_not(self):
        assert analyze("example_intake", _BUNDLE).agent_runnable is False

    def test_the_json_string_spelling_is_accepted(self):
        """Scalars are strings on the wire and at least one bundle writes the
        serialized form straight into the manifest."""
        bundle = {
            **_BUNDLE,
            "manifest.yaml": "scalars:\n  agent_runnable_flows: '[\"example_orphan\"]'\n",
        }
        assert analyze("example_orphan", bundle).agent_runnable is True

    def test_an_unparseable_value_does_not_claim_runnable(self):
        bundle = {**_BUNDLE, "manifest.yaml": "scalars:\n  agent_runnable_flows: not-json\n"}
        assert analyze("example_orphan", bundle).agent_runnable is False


class TestNothingRunsIt:
    def test_a_flow_no_edge_names_has_no_trigger(self):
        report = analyze("example_orphan", _BUNDLE)
        assert report.triggers == []
        assert report.dynamic_callers == []
        assert report.has_trigger is False
        assert report.in_bundle is True

    def test_a_flow_absent_from_the_bundle_is_marked_as_such(self):
        """An ad-hoc flow on a channel whose bundle never mentions it.

        `in_bundle` is what stops "nothing runs this" being read as "this flow
        is dead" when the bundle was simply the wrong place to look.
        """
        report = analyze("example_not_here", _BUNDLE)
        assert report.in_bundle is False
        assert report.has_trigger is False


class TestTolerance:
    def test_an_empty_bundle_answers_instead_of_raising(self):
        report = analyze("example_child", {})
        assert report.has_trigger is False
        assert report.agent_runnable is False

    def test_an_unparseable_flow_costs_only_its_own_edges(self):
        bundle = {**_BUNDLE, "example_broken.yaml": "name: [oops\n  steps: - - -\n"}
        assert analyze("example_child", bundle).of_kind("flow")

    def test_an_unparseable_manifest_costs_only_the_manifest(self):
        bundle = {**_BUNDLE, "manifest.yaml": "webhooks: [oops\n"}
        report = analyze("example_child", bundle)
        assert report.of_kind("flow")
        assert report.of_kind("webhook") == []

    def test_a_yaml_in_a_subdirectory_is_not_read_as_a_flow(self):
        """Flows install from the bundle root only; a nested YAML is data."""
        bundle = {
            **_BUNDLE,
            "code/rules/config.yaml": (
                "name: example_nested\nsteps:\n"
                "- id: go\n  activity: foundation.workflow.start_flow\n"
                "  args: {flow_name: example_child}\n"
            ),
        }
        callers = [t.detail["flow"] for t in analyze("example_child", bundle).of_kind("flow")]
        assert callers == ["example_tick"]

    def test_reserved_entries_are_not_read_as_flows(self):
        bundle = {
            **_BUNDLE,
            "strings.yaml": (
                "name: strings\nsteps:\n"
                "- id: go\n  activity: foundation.workflow.start_flow\n"
                "  args: {flow_name: example_child}\n"
            ),
        }
        callers = [t.detail["flow"] for t in analyze("example_child", bundle).of_kind("flow")]
        assert callers == ["example_tick"]


class TestSerialisation:
    def test_the_dict_carries_every_field_an_agent_branches_on(self):
        data = analyze(
            "example_tick", _BUNDLE, [_LIVE_SCHEDULE], app="example-app", semver="1.0.0"
        ).to_dict()
        assert set(data) == {
            "flow_name",
            "app",
            "semver",
            "in_bundle",
            "has_trigger",
            "agent_runnable",
            "triggers",
            "dynamic_callers",
        }
        assert data["app"] == "example-app"
        assert data["semver"] == "1.0.0"
        assert data["has_trigger"] is True
        assert data["triggers"][0]["kind"] == "schedule"
        assert data["triggers"][0]["summary"]


class TestRendering:
    """The `flow get` block itself — the sentence a reader actually gets."""

    @staticmethod
    def _render(report, error=None):
        from popcorn_cli.commands.flow import _render_triggers

        return "\n".join(_render_triggers(report, error))

    def test_no_trigger_is_stated_outright_not_left_as_a_blank_list(self):
        out = self._render(analyze("example_orphan", _BUNDLE))
        assert "nothing in this channel's bundle starts this flow" in out

    def test_the_absence_of_a_schedule_is_said_in_words(self):
        out = self._render(analyze("example_child", _BUNDLE))
        assert "no schedule of its own" in out

    def test_a_scheduled_flow_does_not_also_claim_it_has_no_schedule(self):
        out = self._render(analyze("example_tick", _BUNDLE, [_LIVE_SCHEDULE]))
        assert "no schedule of its own" not in out
        assert "every 180s" in out

    def test_operator_only_is_spelled_out_for_a_flow_the_agent_cannot_run(self):
        out = self._render(analyze("example_intake", _BUNDLE))
        assert "operator-only" in out

    def test_a_dynamic_launcher_prints_after_the_verdict(self):
        bundle = {
            **_BUNDLE,
            "example_cta.yaml": (
                "name: example_cta\nsteps:\n"
                "- id: launch\n"
                "  activity: foundation.workflow.start_flow\n"
                "  args: {flow_name: $steps.resolve.output.flow_name}\n"
            ),
        }
        out = self._render(analyze("example_orphan", bundle))
        assert "nothing in this channel's bundle starts this flow" in out
        assert out.index("nothing in this channel") < out.index("Unresolved")

    def test_a_failed_lookup_says_why_rather_than_reporting_no_triggers(self):
        """The dangerous failure: an unreadable bundle rendering as "dead"."""
        out = self._render(None, "the channel's app bundle could not be read (404)")
        assert "not checked" in out
        assert "nothing" not in out


class TestCommandEnvelope:
    """`--json` is a stable agent contract: add keys, never rename or drop."""

    @staticmethod
    def _run(argv):
        import json
        from unittest.mock import patch

        from popcorn_cli import registry
        from popcorn_cli.cli import build_parser
        from popcorn_core import operations

        args = build_parser().parse_args(argv)
        flow_response = {"ok": True, "flow": {"id": "example_tick", "name": "example_tick"}}
        bundle = {
            "app": "example-app",
            "bound_semver": "1.0.0",
            "files": [{"path": p, "content": c} for p, c in _BUNDLE.items()],
        }
        printed = []
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch.object(operations, "get_flow", return_value=flow_response),
            patch.object(operations, "get_channel_app_files", return_value=bundle) as files,
            patch.object(
                operations, "list_scheduled_flows", return_value={"scheduled_flows": []}
            ) as scheds,
            patch("builtins.print", printed.append),
        ):
            assert registry.dispatch(args)
        return json.loads(printed[0]), files, scheds

    def test_the_existing_payload_survives_alongside_the_new_keys(self):
        out, _, _ = self._run(["--json", "flow", "get", "example_tick", "--channel", "#x"])
        assert out["ok"] is True
        assert out["data"]["flow"] == {"id": "example_tick", "name": "example_tick"}
        assert out["data"]["triggers"]["flow_name"] == "example_tick"
        assert out["data"]["triggers_error"] is None

    def test_no_triggers_skips_both_extra_requests(self):
        out, files, scheds = self._run(
            ["--json", "flow", "get", "example_tick", "--channel", "#x", "--no-triggers"]
        )
        assert files.call_count == 0
        assert scheds.call_count == 0
        assert "triggers" not in out["data"]
        assert out["data"]["flow"]["name"] == "example_tick"

    def test_an_unreadable_bundle_leaves_the_definition_intact(self):
        import json
        from unittest.mock import patch

        from popcorn_cli import registry
        from popcorn_cli.cli import build_parser
        from popcorn_core import operations
        from popcorn_core.errors import APIError

        args = build_parser().parse_args(
            ["--json", "flow", "get", "example_tick", "--channel", "#x"]
        )
        printed = []
        with (
            patch("popcorn_cli.cli._get_client", return_value=object()),
            patch.object(operations, "get_flow", return_value={"flow": {"name": "example_tick"}}),
            patch.object(
                operations, "get_channel_app_files", side_effect=APIError("no app bundle", 404)
            ),
            patch("builtins.print", printed.append),
        ):
            assert registry.dispatch(args)
        out = json.loads(printed[0])
        assert out["data"]["flow"]["name"] == "example_tick"
        assert out["data"]["triggers"] is None
        assert "no app bundle" in out["data"]["triggers_error"]
