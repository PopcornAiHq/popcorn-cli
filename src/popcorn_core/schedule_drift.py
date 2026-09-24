"""Declared-vs-live schedule drift for an installed channel.

A channel's manifest declares `schedules:`, and the installer creates them —
but it does not create them verbatim, and neither does it keep them that way.
Two platform mechanisms rewrite an installed schedule in place:

* the platform pauses and retunes schedules on its own — a mode change, an
  archive, a delete, an app-update lock, the agent switch — recording what it
  did, and why, in the schedule's `note`;
* the deterministic de-peak offset moves a plain daily cron off the minute it
  declares, so a fleet of "daily at 08:00" declarations spreads across the
  hour instead of stacking on `:00`.

So a naive differ is worse than nothing: the common differences are the
intended ones, and burying the real drift under them is how a checker gets
ignored. Everything here exists to tell those apart — `classify` sorts each
difference into one of four classes, and only two of them are worth an alarm.

The de-peak half is read, not derived. Each schedule in the list response
carries an `intended` block beside its armed cadence: what the platform's own
derivation makes of the cadence the schedule carries, computed by the same
function that arms it. Armed equal to intended is the platform confirming the
moved minute, or the interval phase, is its own; this module never computes
either. A copy of that derivation here would be a second implementation with
nothing to notice when the two disagreed, on the one command an operator runs
to be told the truth about a channel.

The served intent says nothing about the manifest — it is derived from the
armed cadence, not the declaration — so matching the hour or the interval
against what the manifest declares stays here, as does the `class:` a
declaration names. `intended.create_time_schedule_class` is deliberately never
read: it is the class the schedule was created with, and a manifest that
re-declares `class:` over a live schedule does not refresh it, so it says
nothing about what the manifest declares today.

What this cannot see: a schedule whose manifest re-declared it from `periodic`
onto a spreading class. The platform keeps judging it by its create-time class,
so it is armed unspread, its intent agrees, and so does its declared cron —
nothing on this surface separates it from a schedule whose derived minute
happens to be the declared one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The declared classes the platform spreads a plain daily cron under. This is
# vocabulary for reading a declaration, not the spread itself: it decides
# whether a manifest asked for a moved minute, and the served intent decides
# which minute. `periodic` — also the class a declaration with no `class:`
# takes — passes its cron through untouched, so a `periodic` cron that differs
# from its declaration is real drift rather than a de-peak.
_CRON_SPREADING_CLASSES = frozenset({"deadline", "window"})
_DEFAULT_SCHEDULE_CLASS = "periodic"

# Markers the platform writes into a schedule's `note` when it changes one
# itself. Their presence is the platform stating that the difference is its
# own doing, which is what separates class 1 from class 4.
#
# Matching free text another system writes is a stopgap and inherently
# fragile: these notes are prose aimed at a human reading a schedule, so a
# reword there silently demotes an explained pause to an unexplained one here
# and this tool starts crying drift at a healthy channel. The durable fix is
# a machine-readable `pause_reason` on the schedule itself, which this would
# read instead of guessing; until that exists, the table below is matched
# case-insensitively as a substring, and a note matching nothing deliberately
# falls through to "unexplained" rather than to the nearest guess.
#
# One entry per note the platform writes, because they do not call for the
# same response: an archive or a lock lifts on its own, a delete never does,
# and the mode-dependent ones are contradicted by a channel reporting prod.
# Folding them together would hand an operator confidently wrong advice —
# telling someone to go un-pause by hand a schedule that resumes itself the
# moment the lock comes off.


@dataclass(frozen=True)
class _PauseMarker:
    """One recognised platform note and what it means for the reader.

    `mode_dependent` is the escalation switch: True for a note that only
    makes sense off prod, so a channel reporting prod contradicts a cadence
    retuned under it. False for the lifecycle and switch notes, which are
    independent of app mode and are therefore taken at face value whatever
    the mode says.

    `resumes` marks a note written when the platform un-paused the schedule.
    It can account for a retuned cadence but never for a pause: a schedule
    whose last platform note is a resume, and which is paused anyway, was
    paused by something that left no note of its own.
    """

    literal: str
    mode_dependent: bool
    pause_summary: str
    resumes: bool = False


_PAUSE_MARKERS: tuple[_PauseMarker, ...] = (
    _PauseMarker(
        literal="channel archived",
        mode_dependent=False,
        pause_summary=(
            "paused because the channel is archived — unarchiving resumes "
            "exactly the schedules carrying this marker"
        ),
    ),
    _PauseMarker(
        literal="channel deleted",
        mode_dependent=False,
        pause_summary=(
            "paused because the channel is deleted — this one is never "
            "auto-resumed, not even by unarchiving, and the schedule is torn "
            "down with the channel. Nothing to do unless the channel is "
            "meant to come back, which is a re-install rather than an "
            "un-pause"
        ),
    ),
    _PauseMarker(
        literal="app updates locked",
        mode_dependent=False,
        pause_summary=(
            "paused because app updates are locked on this channel — "
            "releasing the lock resumes it, so this needs no manual un-pause"
        ),
    ),
    _PauseMarker(
        literal="app_agent off",
        mode_dependent=False,
        pause_summary=(
            "paused because the channel's app agents are switched off — "
            "switching them back on resumes it. Independent of app mode: a "
            "prod channel with the agents off is a deliberate state"
        ),
    ),
    _PauseMarker(
        literal="app_mode not prod",
        mode_dependent=True,
        pause_summary="paused because the channel is not in prod",
    ),
    _PauseMarker(
        literal="app_mode off",
        mode_dependent=True,
        pause_summary="paused by set_app_mode",
    ),
    _PauseMarker(
        literal="prod-only",
        mode_dependent=True,
        pause_summary=("paused because this schedule only runs in prod, and the channel is not"),
    ),
    # Last, so every more specific note matches first. `set_app_mode` writes
    # its resume note on every mode it leaves running — retuned for test and
    # restored for prod alike — so the note alone does not say which mode
    # the schedule is in; only a cadence that disagrees with the manifest
    # can. That is why the mode escalation waits for the cadence comparison.
    _PauseMarker(
        literal="auto-paused: set_app_mode",
        mode_dependent=True,
        pause_summary="paused by set_app_mode",
    ),
    _PauseMarker(
        literal="auto-resumed: set_app_mode",
        mode_dependent=True,
        pause_summary="resumed by set_app_mode",
        resumes=True,
    ),
)

# The class numbers are part of this tool's interface rather than an internal
# detail — `--json` consumers switch on `drift_class`, and the exit-code rule
# below is stated in terms of them — so they must stay stable.
#
# Class 1 is "the platform says it did this", of which app mode is only the
# most common case — an archive, a delete, an update lock and the agent
# switch all land here too. The name is kept because consumers switch on the
# number; the `summary` is what distinguishes one cause from another, and it
# is written to say what the reader should do about each.
CLASS_APP_MODE = 1
CLASS_DEPEAK = 2
CLASS_PAUSED = 3
CLASS_DRIFT = 4

# Classes 1 and 2 are the platform doing what it says it does. Only 3 and 4
# are worth a non-zero exit.
ALARMING_CLASSES = frozenset({CLASS_PAUSED, CLASS_DRIFT})


@dataclass(frozen=True)
class Finding:
    """One schedule's verdict. `drift_class` is None when nothing differs."""

    slug: str
    drift_class: int | None
    summary: str
    declared: str | None = None
    live: str | None = None

    @property
    def alarming(self) -> bool:
        return self.drift_class in ALARMING_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "drift_class": self.drift_class,
            "summary": self.summary,
            "declared": self.declared,
            "live": self.live,
        }


@dataclass
class DriftReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def alarming(self) -> list[Finding]:
        return [f for f in self.findings if f.alarming]

    @property
    def explained(self) -> list[Finding]:
        return [f for f in self.findings if f.drift_class is not None and not f.alarming]

    @property
    def clean(self) -> list[Finding]:
        return [f for f in self.findings if f.drift_class is None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "alarming": len(self.alarming),
            "explained": len(self.explained),
            "clean": len(self.clean),
        }


def _cadence(interval: Any, cron: Any) -> str:
    """A declaration or a live spec as one comparable string."""
    if interval is not None:
        return f"interval {interval}s"
    if cron:
        return f"cron {cron}"
    return "(none)"


def _platform_note(note: str | None) -> _PauseMarker | None:
    """Which platform marker a note carries, if any."""
    text = (note or "").lower()
    for marker in _PAUSE_MARKERS:
        if marker.literal in text:
            return marker
    return None


def _app_mode_verdict(marker: _PauseMarker | None, app_mode: str | None) -> bool | None:
    """Does a platform marker actually explain a difference on this channel?

    None when no marker is present. True when the marker accounts for it.
    False for the one case worth escalating rather than excusing: a
    mode-dependent marker on a channel that reports `prod`. The marker says
    the platform retuned this schedule for a non-prod mode and the channel is
    not in one, so either the mode changed without the schedule being
    restored or the note is stale — both are real findings, and treating the
    marker as a blanket excuse would hide exactly the state that needs
    looking at.

    A marker that is not mode-dependent is unconditional: archiving, a
    delete, an update lock and the agent switch are all independent of app
    mode, so prod contradicts none of them and a schedule paused under one is
    the system working.
    """
    if marker is None:
        return None
    if not marker.mode_dependent:
        return True
    if app_mode is None:
        # The scalar is unset or unreadable. The marker is still the
        # platform's own statement, so take it — but the caller says so in
        # the summary rather than implying the mode was checked.
        return True
    return app_mode.lower() != "prod"


def classify(
    declared: list[dict[str, Any]],
    live: list[dict[str, Any]],
    app_mode: str | None = None,
) -> DriftReport:
    """Sort every declared-vs-live difference into one of the four classes.

    `declared` is the bound manifest's `schedules:` list; `live` is the
    scheduled-flow list response, each item carrying its served `intended`
    block — the caller refuses a response without one rather than letting
    this read an absent intent as a disagreeing one. They are matched by
    `slug`, which is the identity the installer keys on.

    A live schedule with no declaration is reported as clean rather than as
    drift: the installer only deletes name-keyed schedules it owns and leaves
    member-created ones alone, so an extra live schedule is not evidence of
    anything going wrong.
    """
    report = DriftReport()
    live_by_slug = {item.get("slug"): item for item in live if item.get("slug")}

    for entry in declared:
        slug = entry.get("slug")
        if not slug:
            # The installer skips a declaration with no slug, so there is
            # nothing live to compare it against and nothing to report.
            continue

        declared_cadence = _cadence(entry.get("interval"), entry.get("cron"))
        item = live_by_slug.get(slug)

        if item is None:
            report.findings.append(
                Finding(
                    slug=slug,
                    drift_class=CLASS_DRIFT,
                    summary=(
                        "declared by the manifest but not installed on the "
                        "channel — it has never fired and never will"
                    ),
                    declared=declared_cadence,
                    live=None,
                )
            )
            continue

        live_cadence = _cadence(item.get("interval_seconds"), item.get("cron_expr"))
        marker = _platform_note(item.get("note"))

        if item.get("paused"):
            report.findings.append(
                _classify_pause(slug, marker, app_mode, declared_cadence, live_cadence)
            )
            continue

        finding = _classify_cadence(
            slug=slug,
            entry=entry,
            item=item,
            declared_cadence=declared_cadence,
            live_cadence=live_cadence,
            marker=marker,
            app_mode=app_mode,
        )
        report.findings.append(finding)

    return report


def _classify_pause(
    slug: str,
    marker: _PauseMarker | None,
    app_mode: str | None,
    declared_cadence: str,
    live_cadence: str,
) -> Finding:
    """A paused schedule, which the manifest always declares running."""
    live = live_cadence + ", paused"
    if marker is None or marker.resumes:
        return Finding(
            slug=slug,
            drift_class=CLASS_PAUSED,
            summary=(
                "paused, and nothing says why — the manifest "
                "declares it should run. Un-pausing is a manual "
                "step that lives in no repo, so this stays paused "
                "until someone does it"
            ),
            declared=declared_cadence,
            live=live,
        )
    if _app_mode_verdict(marker, app_mode) is False:
        return _contradicted_by_mode(slug, marker, app_mode, declared_cadence, live)
    return Finding(
        slug=slug,
        drift_class=CLASS_APP_MODE,
        summary=_explained_pause(marker, app_mode),
        declared=declared_cadence,
        live=live,
    )


def _contradicted_by_mode(
    slug: str,
    marker: _PauseMarker,
    app_mode: str | None,
    declared_cadence: str,
    live: str,
) -> Finding:
    """A mode-dependent note on a channel whose mode says otherwise."""
    return Finding(
        slug=slug,
        drift_class=CLASS_DRIFT,
        summary=(
            f"note says {marker.literal!r}, but this channel's "
            f"popcorn.app_mode is {app_mode!r} — the schedule was "
            "retuned for a mode the channel is no longer in, so "
            "nothing has restored it"
        ),
        declared=declared_cadence,
        live=live,
    )


def _explained_pause(marker: _PauseMarker, app_mode: str | None) -> str:
    """The pause's own account of itself, plus the mode that confirms it.

    Only a mode-dependent marker gets the app_mode annotation: on the
    others the mode neither confirms nor contradicts anything, so quoting it
    would suggest a check that was not made.
    """
    if not marker.mode_dependent:
        return marker.pause_summary
    if app_mode is None:
        return (
            f"{marker.pause_summary} (note says {marker.literal!r}); "
            "popcorn.app_mode could not be read to confirm the mode"
        )
    return f"{marker.pause_summary} — this channel's app_mode is {app_mode!r}"


def _only_minute_moved(declared_cron: str, live_cron: str) -> bool:
    """Do two crons differ in nothing but a fixed minute?

    That is the whole of what a spread changes, so it is the shape a de-peak
    has to take. Which minute is right is not decided here — the served
    intent does that — only that the hour and the day fields survived.
    """
    declared, live = declared_cron.split(), live_cron.split()
    return (
        len(declared) == len(live) == 5
        and declared[0].isdigit()
        and live[0].isdigit()
        and declared[1:] == live[1:]
    )


def _classify_cadence(
    *,
    slug: str,
    entry: dict[str, Any],
    item: dict[str, Any],
    declared_cadence: str,
    live_cadence: str,
    marker: _PauseMarker | None,
    app_mode: str | None,
) -> Finding:
    """Compare the cadence of a live, unpaused schedule against its declaration.

    Two comparisons, against two different sources. The manifest is judged
    against the armed cadence on the fields it declares — the hour, the
    interval. The armed minute and phase are judged against the served
    `intended` block, because only the platform knows what they should be.
    """
    intended = item.get("intended") or {}
    declared_interval = entry.get("interval")
    declared_cron = entry.get("cron")
    live_interval = item.get("interval_seconds")
    live_cron = item.get("cron_expr")

    if declared_interval is not None and live_interval is not None:
        if declared_interval != live_interval:
            return _cadence_difference(slug, declared_cadence, live_cadence, marker, app_mode)
        armed_phase = item.get("offset_seconds")
        intended_phase = intended.get("offset_seconds")
        if armed_phase != intended_phase:
            # No marker excuses this: a mode change that retunes an interval
            # re-derives its phase, so a phase that is not the platform's own
            # was put there by something other than the platform.
            return Finding(
                slug=slug,
                drift_class=CLASS_DRIFT,
                summary=(
                    f"fires at phase {armed_phase!r}s inside its interval, but "
                    f"the platform derives {intended_phase!r}s for this schedule "
                    "— it is armed off the platform's own spread"
                ),
                declared=declared_cadence,
                live=f"{live_cadence}, phase {armed_phase!r}s",
            )
        return Finding(slug=slug, drift_class=None, summary="matches the manifest")

    if declared_cron and live_cron:
        declared_expr = str(declared_cron).strip()
        live_expr = str(live_cron).strip()
        intended_expr = str(intended.get("cron_expr") or "").strip()
        spreads = str(entry.get("class") or _DEFAULT_SCHEDULE_CLASS) in _CRON_SPREADING_CLASSES
        armed_as_intended = live_expr == intended_expr

        if live_expr == declared_expr:
            if armed_as_intended or not spreads:
                # A `periodic` declaration asks for its cron verbatim, which
                # is what is armed; the intent disagreeing then only reflects
                # the class the schedule was created with, not this manifest.
                return Finding(slug=slug, drift_class=None, summary="matches the manifest")
            return Finding(
                slug=slug,
                drift_class=CLASS_DRIFT,
                summary=(
                    f"armed at its declared {declared_expr!r}, but the platform "
                    f"means it to fire at {intended_expr!r} — the manifest "
                    "declares a spreading class and the spread was never applied"
                ),
                declared=declared_cadence,
                live=live_cadence,
            )
        if spreads and armed_as_intended and _only_minute_moved(declared_expr, live_expr):
            return Finding(
                slug=slug,
                drift_class=CLASS_DEPEAK,
                summary=(
                    f"de-peaked off {declared_expr!r} to {live_expr!r} — the "
                    "minute the platform reports deriving for this schedule"
                ),
                declared=declared_cadence,
                live=live_cadence,
            )
        return _cadence_difference(slug, declared_cadence, live_cadence, marker, app_mode)

    # One side is an interval and the other a cron, or a side is missing
    # entirely. Nothing derives one from the other.
    return Finding(
        slug=slug,
        drift_class=CLASS_DRIFT,
        summary="the live cadence is a different kind from the declared one",
        declared=declared_cadence,
        live=live_cadence,
    )


def _cadence_difference(
    slug: str,
    declared_cadence: str,
    live_cadence: str,
    marker: _PauseMarker | None,
    app_mode: str | None,
) -> Finding:
    """A cadence that differs for a reason the platform's intent does not explain.

    This is where a mode-dependent note meets the mode: a cadence retuned
    under one, on a channel reporting prod, is the state nothing restored.
    Only here and on a pause — a note beside a cadence that already matches
    the manifest contradicts nothing, whatever the mode.
    """
    if marker is not None and _app_mode_verdict(marker, app_mode) is False:
        return _contradicted_by_mode(slug, marker, app_mode, declared_cadence, live_cadence)
    if marker is not None:
        mode = f" (app_mode {app_mode!r})" if app_mode else ""
        return Finding(
            slug=slug,
            drift_class=CLASS_APP_MODE,
            summary=(
                f"retuned by the platform, note says {marker.literal!r}{mode} — the "
                "next install restores the manifest's cadence"
            ),
            declared=declared_cadence,
            live=live_cadence,
        )
    return Finding(
        slug=slug,
        drift_class=CLASS_DRIFT,
        summary=(
            "runs on a different cadence than the manifest declares, and nothing accounts for it"
        ),
        declared=declared_cadence,
        live=live_cadence,
    )
