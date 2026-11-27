"""Unit tests for message-row direction resolution.

These run without a browser. WhatsApp Web marks only the first message of a
consecutive run with a tail-in / tail-out marker, so continuation rows must
inherit direction from their neighbours; resolving each row in isolation is
what caused incoming counts to be inflated.
"""

from __future__ import annotations

from wa_meta_ai.browser import incoming_rows, resolve_rows


def row(**kwargs):
    base = {
        "id": kwargs.pop("id", "m"),
        "tailIn": False,
        "tailOut": False,
        "youLabel": False,
        "system": False,
        "icons": [],
        "text": "",
        "media": [],
    }
    base.update(kwargs)
    return base


def directions(rows):
    return [r["direction"] for r in resolve_rows(rows)]


def test_explicit_markers_win():
    rows = [row(id="a", tailIn=True), row(id="b", tailOut=True)]
    assert directions(rows) == ["in", "out"]


def test_continuation_rows_inherit_previous_marker():
    # One outgoing run of three messages, then an incoming run of three.
    rows = [
        row(id="a", tailOut=True),
        row(id="b"),
        row(id="c"),
        row(id="d", tailIn=True),
        row(id="e"),
        row(id="f"),
    ]
    assert directions(rows) == ["out", "out", "out", "in", "in", "in"]


def test_window_opening_mid_run_backfills_from_next_marker():
    # The visible window starts two messages into an incoming run.
    rows = [row(id="a"), row(id="b"), row(id="c", tailIn=True), row(id="d")]
    assert directions(rows) == ["in", "in", "in", "in"]


def test_no_markers_at_all_is_unknown_not_incoming():
    rows = [row(id="a"), row(id="b")]
    assert directions(rows) == ["unknown", "unknown"]
    assert incoming_rows(resolve_rows(rows)) == []


def test_system_rows_are_unknown_and_excluded():
    rows = [row(id="a", tailIn=True), row(id="b", system=True), row(id="c")]
    resolved = resolve_rows(rows)
    assert resolved[1]["direction"] == "unknown"
    assert [r["id"] for r in incoming_rows(resolved)] == ["a", "c"]


def test_incoming_rows_excludes_outgoing_media():
    rows = [
        row(id="out1", tailOut=True, media=[{"src": "u1", "kind": "image"}]),
        row(id="in1", tailIn=True, media=[{"src": "u2", "kind": "image"}]),
    ]
    incoming = incoming_rows(resolve_rows(rows))
    assert [r["id"] for r in incoming] == ["in1"]
    assert incoming[0]["media"][0]["src"] == "u2"


def test_incoming_count_matches_real_runs_not_raw_rows():
    # The bug that reported 8 incoming on a chat with 2: a 5-row incoming run
    # plus a 3-row outgoing run produced 8 "incoming" rows.
    rows = [row(id=f"in{i}", **({"tailIn": True} if i == 0 else {})) for i in range(5)]
    rows += [row(id=f"out{i}", **({"tailOut": True} if i == 0 else {})) for i in range(3)]
    assert len(incoming_rows(resolve_rows(rows))) == 5


def test_resolution_does_not_mutate_input():
    rows = [row(id="a", tailIn=True)]
    resolve_rows(rows)
    assert "direction" not in rows[0]
