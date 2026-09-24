"""Declared-vs-live schedule drift classification.

The platform serves, per schedule, the cadence it intends beside the one that
is armed, so none of these tests computes a de-peaked minute or an interval
phase. Where a spread value appears it is an arbitrary number standing in for
what a server reported — the tests that matter pick one no local derivation
would produce, so a classifier that quietly went back to deriving would fail
them rather than agree with itself.
"""

from __future__ import annotations

import pytest

from popcorn_core import schedule_drift
from popcorn_core.schedule_drift import (
    CLASS_APP_MODE,
    CLASS_DEPEAK,
    CLASS_DRIFT,
    CLASS_PAUSED,
    classify,
)


def _live(slug: str, **over: object) -> dict:
    """A live schedule whose served intent agrees with what is armed.

    That is the healthy case, so it is the default; a test that needs the
    platform to disagree with its own arming passes `intended=` with only the
    fields that differ.
    """
    intended_over = over.pop("intended", {})
    item: dict = {
        "schedule_id": f"channel:conv:flow:f:{slug}",
        "slug": slug,
        "cron_expr": None,
        "interval_seconds": 900,
        "offset_seconds": 51,
        "paused": False,
        "note": None,
    }
    item.update(over)
    item["intended"] = {
        "create_time_schedule_class": "periodic",
        "cron_expr": item["cron_expr"],
        "interval_seconds": item["interval_seconds"],
        "offset_seconds": item["offset_seconds"] if item["interval_seconds"] else None,
    }
    item["intended"].update(intended_over)
    return item


def _cron(slug: str, cron: str, **over: object) -> dict:
    return _live(slug, cron_expr=cron, interval_seconds=None, offset_seconds=None, **over)


class TestClassify:
    def test_matching_interval_is_clean(self) -> None:
        report = classify([{"slug": "s", "interval": 900}], [_live("s")])
        assert report.findings[0].drift_class is None
        assert not report.alarming

    def test_depeaked_cron_is_class_2(self) -> None:
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_cron("daily", "17 8 * * *")],
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DEPEAK
        assert not finding.alarming

    def test_the_depeaked_minute_is_the_servers_not_a_local_derivation(self) -> None:
        """Whatever minute the server reports is the one accepted.

        Checked for every minute rather than one, so a classifier deriving
        the minute locally cannot pass by agreeing with the server on a
        single lucky id.
        """
        for minute in range(1, 60):
            report = classify(
                [{"slug": "daily", "cron": "0 8 * * *", "class": "window"}],
                [_cron("daily", f"{minute} 8 * * *")],
            )
            assert report.findings[0].drift_class == CLASS_DEPEAK, minute

    def test_cron_armed_off_its_intent_is_class_4(self) -> None:
        """One minute away from what the platform means is real drift."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_cron("daily", "16 8 * * *", intended={"cron_expr": "17 8 * * *"})],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_depeak_keeps_the_declared_hour(self) -> None:
        """The spread moves the minute only; a different hour is drift even
        when the platform stands behind what is armed."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_cron("daily", "17 9 * * *")],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_spreading_class_left_unspread_is_class_4(self) -> None:
        """Armed as declared, but the platform means a moved minute."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_cron("daily", "0 8 * * *", intended={"cron_expr": "17 8 * * *"})],
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DRIFT
        assert "17 8 * * *" in finding.summary

    def test_spread_cron_whose_derived_minute_is_the_declared_one_is_clean(self) -> None:
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_cron("daily", "0 8 * * *")],
        )
        assert report.findings[0].drift_class is None

    def test_periodic_cron_differing_is_class_4_not_depeak(self) -> None:
        """A `periodic` cron passes through, so a difference is never a de-peak
        — even one the platform's intent agrees with, which is what a
        schedule created spread and re-declared `periodic` looks like."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "periodic"}],
            [_cron("daily", "17 8 * * *")],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_periodic_cron_armed_as_declared_is_clean_whatever_the_intent(self) -> None:
        """The intent reflects the class a schedule was created with, which a
        `periodic` re-declaration does not change. Armed as declared is the
        manifest's own answer, so the stale class must not raise an alarm."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "periodic"}],
            [_cron("daily", "0 8 * * *", intended={"cron_expr": "17 8 * * *"})],
        )
        assert report.findings[0].drift_class is None

    def test_class_defaults_to_periodic(self) -> None:
        """No `class:` in the declaration means `periodic`, so no spreading."""
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *"}],
            [_cron("daily", "17 8 * * *")],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_create_time_class_is_never_read(self) -> None:
        """It is provenance, not the manifest's class today.

        Served as spreading on a declaration that is `periodic`, and as
        `periodic` on one that spreads; neither may change the verdict.
        """
        periodic = classify(
            [{"slug": "daily", "cron": "0 8 * * *"}],
            [_cron("daily", "17 8 * * *", intended={"create_time_schedule_class": "window"})],
        )
        assert periodic.findings[0].drift_class == CLASS_DRIFT
        spreading = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "window"}],
            [_cron("daily", "17 8 * * *", intended={"create_time_schedule_class": "periodic"})],
        )
        assert spreading.findings[0].drift_class == CLASS_DEPEAK

    def test_interval_armed_at_its_intended_phase_is_clean(self) -> None:
        report = classify([{"slug": "tick", "interval": 900}], [_live("tick", offset_seconds=733)])
        assert report.findings[0].drift_class is None

    def test_interval_armed_off_its_intended_phase_is_class_4(self) -> None:
        """The interval matches, so only the phase can say anything is wrong."""
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", offset_seconds=12, intended={"offset_seconds": 733})],
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DRIFT
        assert finding.alarming
        assert "733" in finding.summary

    def test_an_unphased_interval_is_class_4(self) -> None:
        """No armed phase at all where the platform derives one."""
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", offset_seconds=None, intended={"offset_seconds": 733})],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_a_marker_does_not_excuse_a_wrong_phase(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [
                _live(
                    "tick",
                    offset_seconds=12,
                    note="auto-resumed: set_app_mode",
                    intended={"offset_seconds": 733},
                )
            ],
            app_mode="test",
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_retuned_interval_with_marker_is_class_1(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", interval_seconds=180, note="auto-resumed: set_app_mode")],
            app_mode="test",
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_APP_MODE
        assert not finding.alarming

    def test_retuned_interval_without_marker_is_class_4(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", interval_seconds=180)],
            app_mode="test",
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_paused_without_explanation_is_class_3(self) -> None:
        report = classify([{"slug": "tick", "interval": 900}], [_live("tick", paused=True)])
        finding = report.findings[0]
        assert finding.drift_class == CLASS_PAUSED
        assert finding.alarming

    def test_paused_by_app_mode_is_class_1(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="auto-paused: app_mode off")],
            app_mode="off",
        )
        assert report.findings[0].drift_class == CLASS_APP_MODE

    def test_paused_by_archive_is_explained_regardless_of_mode(self) -> None:
        """Archiving is independent of app mode, so prod does not contradict it."""
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="auto-paused: channel archived")],
            app_mode="prod",
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_APP_MODE
        assert not finding.alarming

    def test_app_mode_marker_on_a_prod_channel_is_class_4(self) -> None:
        """The escalation that makes app_mode worth reading at all.

        A schedule retuned for a non-prod mode, on a channel now reporting
        prod, is the state nothing has restored — excusing it on the marker
        alone would hide exactly the case worth looking at.
        """
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", interval_seconds=180, note="auto-resumed: set_app_mode")],
            app_mode="prod",
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DRIFT
        assert finding.alarming

    def test_unreadable_app_mode_still_takes_the_marker(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", interval_seconds=180, note="auto-resumed: set_app_mode")],
            app_mode=None,
        )
        assert report.findings[0].drift_class == CLASS_APP_MODE

    def test_declared_but_not_installed_is_class_4(self) -> None:
        report = classify([{"slug": "ghost", "interval": 900}], [])
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DRIFT
        assert finding.live is None
        assert finding.alarming

    def test_live_but_undeclared_is_not_reported(self) -> None:
        """Member-created schedules are not the bundle's to account for."""
        report = classify([], [_live("member-made")])
        assert report.findings == []

    def test_declaration_without_slug_is_skipped(self) -> None:
        """The installer skips these too, so there is nothing live to compare."""
        report = classify([{"flow": "f", "interval": 900}], [_live("s")])
        assert report.findings == []

    def test_cadence_kind_change_is_class_4(self) -> None:
        report = classify(
            [{"slug": "s", "cron": "0 8 * * *"}],
            [_live("s", interval_seconds=900, cron_expr=None)],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_report_partitions_every_finding(self) -> None:
        report = classify(
            [
                {"slug": "ok", "interval": 900},
                {"slug": "paused", "interval": 900},
                {"slug": "retuned", "interval": 900},
            ],
            [
                _live("ok"),
                _live("paused", paused=True),
                _live("retuned", interval_seconds=180, note="auto-resumed: set_app_mode"),
            ],
            app_mode="test",
        )
        assert len(report.clean) == 1
        assert len(report.alarming) == 1
        assert len(report.explained) == 1
        assert len(report.findings) == 3

    def test_to_dict_is_json_safe(self) -> None:
        import json

        report = classify([{"slug": "s", "interval": 900}], [_live("s", paused=True)])
        assert json.loads(json.dumps(report.to_dict()))["alarming"] == 1


# The platform's auto-pause notes, paired with the app mode each is written
# under. Mode-independent notes (an archive, a delete, an update lock, the
# agent switch) are legitimate on a prod channel; mode-dependent ones say
# "not prod" in so many words, so prod contradicts them.
_MODE_INDEPENDENT = [
    ("auto-paused: channel archived", "prod"),
    ("auto-paused: channel deleted", "prod"),
    ("auto-paused: app updates locked", "prod"),
    ("auto-paused: app_agent off", "prod"),
]
_MODE_DEPENDENT = [
    ("auto-paused: app_mode off", "off"),
    ("auto-paused: app_mode not prod", "test"),
    ("auto-paused: investigator is prod-only", "test"),
]


class TestPauseNotes:
    """Every auto-pause note the platform writes, and the fallback.

    The bug these pin: the recogniser knew two of the notes, so a schedule
    paused by any of the others was reported as "paused, and nothing says
    why" — an alarm whose remediation (go un-pause it by hand) is wrong for
    a lock, which releases itself, and for a delete, which must never be
    resumed. The notes below are the literals the platform writes; they are
    prose it owns, so this suite is also the tripwire for a reword.
    """

    @pytest.mark.parametrize("note,app_mode", _MODE_INDEPENDENT + _MODE_DEPENDENT)
    def test_every_platform_pause_is_explained(self, note: str, app_mode: str) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note=note)],
            app_mode=app_mode,
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_APP_MODE
        assert not finding.alarming

    @pytest.mark.parametrize("note,_mode", _MODE_INDEPENDENT)
    def test_mode_independent_pauses_survive_prod(self, note: str, _mode: str) -> None:
        """A lock, a delete, an archive and the agent switch are not modes.

        Escalating these on a prod channel would be the same false alarm in
        a new costume: prod contradicts none of them.
        """
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note=note)],
            app_mode="prod",
        )
        assert report.findings[0].drift_class == CLASS_APP_MODE

    @pytest.mark.parametrize("note,_mode", _MODE_DEPENDENT)
    def test_mode_dependent_pauses_are_escalated_on_prod(self, note: str, _mode: str) -> None:
        """These say "not prod" in so many words, so prod contradicts them."""
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note=note)],
            app_mode="prod",
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DRIFT
        assert finding.alarming

    def test_lock_does_not_advise_a_manual_unpause(self) -> None:
        """The wording is the fix, not just the class.

        A locked channel's schedules resume when the lock comes off, so any
        advice to go un-pause one by hand sends an operator to do work that
        undoes itself.
        """
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="auto-paused: app updates locked")],
            app_mode="prod",
        )
        summary = report.findings[0].summary
        assert "lock" in summary
        assert "no manual un-pause" in summary

    def test_delete_says_it_is_never_resumed(self) -> None:
        """The opposite advice to the lock's, so it must not share its bucket."""
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="auto-paused: channel deleted")],
            app_mode="prod",
        )
        summary = report.findings[0].summary
        assert "deleted" in summary
        assert "never" in summary

    def test_each_note_reads_differently(self) -> None:
        """Recognising a note is only half of it — it has to say which one."""
        summaries = set()
        for note, app_mode in _MODE_INDEPENDENT + _MODE_DEPENDENT:
            report = classify(
                [{"slug": "tick", "interval": 900}],
                [_live("tick", paused=True, note=note)],
                app_mode=app_mode,
            )
            summaries.add(report.findings[0].summary)
        # `app_mode off` and the generic `set_app_mode` marker deliberately
        # read alike, so only the distinct causes are counted.
        assert len(summaries) == len(_MODE_INDEPENDENT) + len(_MODE_DEPENDENT)

    def test_an_unknown_note_still_falls_through_to_unexplained(self) -> None:
        """The fifth note someone adds tomorrow must degrade, not mis-bucket.

        Free-text matching cannot be exhaustive, so the fallback is the part
        that has to hold: an unrecognised reason is reported as unexplained
        rather than quietly excused by the nearest marker.
        """
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="auto-paused: quota exhausted")],
            app_mode="prod",
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_PAUSED
        assert finding.alarming

    def test_note_matching_ignores_case(self) -> None:
        report = classify(
            [{"slug": "tick", "interval": 900}],
            [_live("tick", paused=True, note="Auto-Paused: App Updates Locked")],
            app_mode="prod",
        )
        assert report.findings[0].drift_class == CLASS_APP_MODE


class TestAlarmingClasses:
    def test_class_numbers_are_stable(self) -> None:
        """`--json` consumers switch on these, so they never move."""
        assert (CLASS_APP_MODE, CLASS_DEPEAK, CLASS_PAUSED, CLASS_DRIFT) == (1, 2, 3, 4)

    def test_only_three_and_four_alarm(self) -> None:
        """The agreed acceptance: exit non-zero only on class 3 and 4."""
        assert {CLASS_PAUSED, CLASS_DRIFT} == schedule_drift.ALARMING_CLASSES
