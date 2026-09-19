"""Declared-vs-live schedule drift classification.

The offset vectors below are the load-bearing tests. `stable_offset_seconds`
is a port of the backend's function, and a port that has silently drifted from
its original would not fail anywhere else: every classification would still be
self-consistent, and every de-peaked schedule would be reported as class-4
drift that a reader then has to disprove by hand. Pinning offsets computed
with the backend's own implementation is what makes the port falsifiable.
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
    expected_cron,
    parse_daily_cron,
    stable_offset_seconds,
)

# Three synthetic schedule ids. They name no real channel — the derivation
# only reads the id as bytes, so a made-up id exercises it exactly as a live
# one would. What makes these vectors worth anything is where the expected
# values come from: each was computed by running the backend's own
# `stable_offset_seconds` against these exact ids, so the port is pinned
# against its original rather than merely against itself. Change an id and the
# expected value has to be recomputed the same way, never read back off this
# port.
#
# `_CLEANUP_ID` deriving 1 is the useful edge of the set: 1 is the bottom of
# the `[1, modulus)` range, so that vector also pins the fact that the
# derivation never returns 0.
_CHANNEL = "channel:00000000-0000-4000-8000-000000000001"
_BRIEFING_ID = f"{_CHANNEL}:flow:daily_briefing:daily-briefing"
_TICK_ID = f"{_CHANNEL}:flow:sweep_tick:sweep-tick"
_CLEANUP_ID = f"{_CHANNEL}:flow:nightly_cleanup:nightly-cleanup"


def _live(slug: str, **over: object) -> dict:
    item = {
        "schedule_id": f"channel:conv:flow:f:{slug}",
        "slug": slug,
        "cron_expr": None,
        "interval_seconds": 900,
        "paused": False,
        "note": None,
    }
    item.update(over)
    return item


class TestStableOffset:
    def test_never_zero_above_modulus_one(self) -> None:
        """A derived 0 is dropped on the wire, so the range starts at 1."""
        for i in range(200):
            assert stable_offset_seconds(f"channel:c:flow:f:s{i}", 60) >= 1

    def test_in_range(self) -> None:
        for i in range(200):
            assert 1 <= stable_offset_seconds(f"channel:c:flow:f:s{i}", 180) < 180

    def test_modulus_one_is_the_only_zero(self) -> None:
        assert stable_offset_seconds("anything", 1) == 0

    def test_stable_across_calls(self) -> None:
        """Not `hash()` — PYTHONHASHSEED would make that differ per process."""
        assert stable_offset_seconds(_TICK_ID, 180) == stable_offset_seconds(_TICK_ID, 180)

    def test_rejects_zero_modulus(self) -> None:
        with pytest.raises(ValueError):
            stable_offset_seconds("x", 0)


class TestPortedOffsetVectors:
    """The port against values computed with the backend's own implementation."""

    def test_briefing_cron_minute(self) -> None:
        assert stable_offset_seconds(_BRIEFING_ID, 60) == 45

    def test_tick_interval_phase(self) -> None:
        assert stable_offset_seconds(_TICK_ID, 180) == 51

    def test_cleanup_cron_minute(self) -> None:
        """Also the bottom of the range: the derivation never returns 0."""
        assert stable_offset_seconds(_CLEANUP_ID, 60) == 1

    def test_briefing_expected_cron_matches_backend(self) -> None:
        assert expected_cron(_BRIEFING_ID, "0 8 * * *", "deadline") == "45 8 * * *"

    def test_cleanup_expected_cron_matches_backend(self) -> None:
        assert expected_cron(_CLEANUP_ID, "0 9 * * *", "deadline") == "1 9 * * *"


class TestParseDailyCron:
    @pytest.mark.parametrize(
        "expr,want",
        [
            ("0 8 * * *", (8, 0)),
            ("  30 9 * * *  ", (9, 30)),
            ("59 23 * * *", (23, 59)),
        ],
    )
    def test_plain_daily(self, expr: str, want: tuple[int, int]) -> None:
        assert parse_daily_cron(expr) == want

    @pytest.mark.parametrize(
        "expr",
        [
            "0,30 8 * * *",  # minute list
            "*/15 * * * *",  # step
            "0 8 * * 1",  # day restriction
            "0 8 1 * *",  # day-of-month restriction
            "60 8 * * *",  # out of range
            "0 24 * * *",  # out of range
            "not a cron",
        ],
    )
    def test_not_plain_daily(self, expr: str) -> None:
        assert parse_daily_cron(expr) is None


class TestExpectedCron:
    def test_periodic_passes_through_unspread(self) -> None:
        """The default class. A `periodic` cron is never de-peaked."""
        assert expected_cron(_BRIEFING_ID, "0 8 * * *", "periodic") is None

    def test_window_spreads(self) -> None:
        assert expected_cron(_BRIEFING_ID, "0 8 * * *", "window") == "45 8 * * *"

    def test_hour_is_preserved(self) -> None:
        assert expected_cron(_BRIEFING_ID, "0 17 * * *", "deadline") == "45 17 * * *"

    def test_non_daily_shape_unspread_even_under_deadline(self) -> None:
        assert expected_cron(_BRIEFING_ID, "*/15 * * * *", "deadline") is None


class TestClassify:
    def test_matching_interval_is_clean(self) -> None:
        report = classify([{"slug": "s", "interval": 900}], [_live("s")])
        assert report.findings[0].drift_class is None
        assert not report.alarming

    def test_depeaked_cron_is_class_2(self) -> None:
        sid = "channel:conv:flow:f:daily"
        want = expected_cron(sid, "0 8 * * *", "deadline")
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_live("daily", cron_expr=want, interval_seconds=None, schedule_id=sid)],
        )
        finding = report.findings[0]
        assert finding.drift_class == CLASS_DEPEAK
        assert not finding.alarming

    def test_cron_off_its_derivation_is_class_4(self) -> None:
        """Exact equality: one minute away from the derivation is real drift."""
        sid = "channel:conv:flow:f:daily"
        want = expected_cron(sid, "0 8 * * *", "deadline")
        assert want is not None
        off_by_one = f"{int(want.split()[0]) - 1} 8 * * *"
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "deadline"}],
            [_live("daily", cron_expr=off_by_one, interval_seconds=None, schedule_id=sid)],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_periodic_cron_differing_is_class_4_not_depeak(self) -> None:
        """A `periodic` cron passes through, so a difference is never a de-peak."""
        sid = "channel:conv:flow:f:daily"
        spread = expected_cron(sid, "0 8 * * *", "deadline")
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *", "class": "periodic"}],
            [_live("daily", cron_expr=spread, interval_seconds=None, schedule_id=sid)],
        )
        assert report.findings[0].drift_class == CLASS_DRIFT

    def test_class_defaults_to_periodic(self) -> None:
        """No `class:` in the declaration means `periodic`, so no spreading."""
        sid = "channel:conv:flow:f:daily"
        spread = expected_cron(sid, "0 8 * * *", "deadline")
        report = classify(
            [{"slug": "daily", "cron": "0 8 * * *"}],
            [_live("daily", cron_expr=spread, interval_seconds=None, schedule_id=sid)],
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
    def test_only_three_and_four_alarm(self) -> None:
        """The agreed acceptance: exit non-zero only on class 3 and 4."""
        assert {CLASS_PAUSED, CLASS_DRIFT} == schedule_drift.ALARMING_CLASSES
