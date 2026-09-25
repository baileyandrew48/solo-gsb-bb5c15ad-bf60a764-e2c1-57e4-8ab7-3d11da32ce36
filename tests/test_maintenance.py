"""Tests for planned-maintenance semantics in the rule engine.

Maintenance windows are named half-open intervals on a device.  Evaluation
merges overlapping/touching windows per device, subtracts them from that
device's truth segments (resetting the sustained duration), and reports the
suppressed pieces separately — each piece split at the source windows'
boundaries so it names only the windows that actually cover it.
"""

from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    attribute_windows,
    evaluate_rule,
    merge_device_windows,
    subtract,
)


def ev(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def minute_series(eid_base, predicate_fn, device="d", metric="m"):
    """One sample per minute for minutes 0..30, value from predicate_fn(minute)."""
    return [
        ev(eid_base + m, m * 60, predicate_fn(m), device, metric)
        for m in range(31)
    ]


def ivs(faults):
    return [(f.start, f.end) for f in faults]


def mw(wid, device, start_min, end_min, name="检修"):
    return MaintenanceWindow(
        id=wid, device=device, start=start_min * 60, end=end_min * 60, name=name
    )


# --------------------------------------------------------------------------- #
# subtract / merge_device_windows primitives
# --------------------------------------------------------------------------- #


def test_subtract_middle_splits_interval():
    assert subtract([Interval(0, 10)], [Interval(3, 6)]) == [
        Interval(0, 3),
        Interval(6, 10),
    ]


def test_subtract_touching_boundary_keeps_interval_intact():
    # half-open: [0,6) minus a window starting exactly at 6 loses nothing
    assert subtract([Interval(0, 6)], [Interval(6, 10)]) == [Interval(0, 6)]
    assert subtract([Interval(6, 10)], [Interval(0, 6)]) == [Interval(6, 10)]


def test_subtract_multiple_windows_and_full_cover():
    assert subtract([Interval(0, 20)], [Interval(2, 4), Interval(8, 12)]) == [
        Interval(0, 2),
        Interval(4, 8),
        Interval(12, 20),
    ]
    assert subtract([Interval(0, 10)], [Interval(0, 10)]) == []
    assert subtract([], [Interval(0, 10)]) == []
    assert subtract([Interval(0, 10)], []) == [Interval(0, 10)]


def test_merge_device_windows_merges_overlap_and_touch_per_device():
    merged = merge_device_windows(
        [
            ("pump-1", 0.0, 60.0),
            ("pump-1", 60.0, 120.0),   # touching -> merges
            ("pump-1", 100.0, 200.0),  # overlapping -> merges
            ("pump-1", 300.0, 360.0),  # real gap -> separate
            ("pump-2", 0.0, 60.0),     # other device never merges
        ]
    )
    assert merged["pump-1"] == [Interval(0, 200), Interval(300, 360)]
    assert merged["pump-2"] == [Interval(0, 60)]


# --------------------------------------------------------------------------- #
# evaluation semantics
# --------------------------------------------------------------------------- #


def test_maintenance_resets_duration_and_reconfirms_after_window():
    # temp >= 80 held on [6,17); maintenance [10,12) cuts it in two.
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 12)],
    )
    # [6,10) is 4 min < 5 min -> dropped; [12,17) is 5 min -> fault
    assert ivs(result.faults) == [(12 * 60, 17 * 60)]
    # duration re-accumulates from the post-window segment start
    assert result.faults[0].confirmed_at == 12 * 60 + 300
    # the cut piece is reported as suppressed, linked to the window
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (10 * 60, 12 * 60)
    ]
    assert result.suppressed[0].maintenance_ids == [1]
    assert result.suppressed[0].device == "d" and result.suppressed[0].metric == "m"


def test_maintenance_without_windows_keeps_legacy_behavior():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events}, horizon=30 * 60
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []


def test_maintenance_on_other_device_does_not_affect_rule():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "other-device", 6, 17)],
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []


def test_touching_maintenance_windows_merge_before_subtraction():
    # two adjacent windows [8,10) and [10,12) act as one [8,12) cut
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 8, 10, "A"), mw(2, "d", 10, 12, "B")],
    )
    assert ivs(result.faults) == [(12 * 60, 17 * 60)]
    # the merged cut is split at the junction: each piece names only the
    # window that actually covers it
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (8 * 60, 10 * 60, [1]),
        (10 * 60, 12 * 60, [2]),
    ]


def test_evidence_never_crosses_maintenance_boundary():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 12)],
    )
    evidence = result.faults[0].evidence[0]
    # held sub-intervals are clipped at the window start
    assert evidence.held_intervals == [Interval(12 * 60, 17 * 60)]
    # only samples at/after the window end witness the surviving fault
    assert [w.ts for w in evidence.events] == [m * 60 for m in range(12, 17)]


def test_maintenance_covering_whole_truth_suppresses_fault():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 10 else 10)  # [6,11)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 5, 12)],
    )
    assert result.faults == []
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (6 * 60, 11 * 60)
    ]


def test_and_rule_maintenance_on_one_condition_device():
    # AND of temp (device d) and vib (device e); maintenance on e only.
    temp = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 60, device="d", metric="temp")
    vib = minute_series(200, lambda m: 3.6 if 6 <= m <= 16 else 1.0, device="e", metric="vib")
    result = evaluate_rule(
        2,
        "AND",
        120,
        [Condition("d", "temp", ">=", 80), Condition("e", "vib", ">", 3)],
        {("d", "temp"): temp, ("e", "vib"): vib},
        horizon=30 * 60,
        maintenance=[mw(1, "e", 10, 12)],
    )
    # truth [6,17) minus [10,12): [6,10) and [12,17), both >= 120s
    assert ivs(result.faults) == [(6 * 60, 10 * 60), (12 * 60, 17 * 60)]
    assert result.faults[0].confirmed_at == 6 * 60 + 120
    assert result.faults[1].confirmed_at == 12 * 60 + 120
    # suppressed piece attributed to the vib channel (device e), not temp
    assert [(s.device, s.metric, s.interval.start, s.interval.end) for s in result.suppressed] == [
        ("e", "vib", 10 * 60, 12 * 60)
    ]


def test_or_rule_suppressed_only_where_rule_truth_is_lost():
    # vib held [3,9), press held [6,14); maintenance on vib's device [4,8).
    # The rule's OR truth survives on [6,8) via press, so only [4,6) is lost.
    vib = minute_series(300, lambda m: 6.0 if 3 <= m <= 8 else 2.0, device="d", metric="vib")
    press = minute_series(400, lambda m: 26.0 if 6 <= m <= 13 else 52.0, device="e", metric="press")
    result = evaluate_rule(
        3,
        "OR",
        0,
        [Condition("d", "vib", ">", 5), Condition("e", "press", "<", 30)],
        {("d", "vib"): vib, ("e", "press"): press},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 4, 8)],
    )
    assert ivs(result.faults) == [(3 * 60, 4 * 60), (6 * 60, 14 * 60)]
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (4 * 60, 6 * 60)
    ]


def test_maintenance_window_with_zero_length_is_ignored():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 10)],
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []


# --------------------------------------------------------------------------- #
# per-segment window attribution
# --------------------------------------------------------------------------- #


def test_separated_windows_attribute_only_their_own_segments():
    # two distant windows on the same device each cut the truth; each
    # suppressed piece must point at its own window only, never at every
    # window of the device
    events = minute_series(100, lambda m: 90 if 6 <= m <= 28 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 8, 10, "A"), mw(2, "d", 20, 22, "B")],
    )
    # truth [6,29) cut at [8,10) and [20,22): [6,8) is too short, the two
    # survivors re-accumulate the duration from their own starts
    assert ivs(result.faults) == [(10 * 60, 20 * 60), (22 * 60, 29 * 60)]
    assert result.faults[0].confirmed_at == 10 * 60 + 300
    assert result.faults[1].confirmed_at == 22 * 60 + 300
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (8 * 60, 10 * 60, [1]),
        (20 * 60, 22 * 60, [2]),
    ]


def test_overlapping_windows_share_attribution_on_the_overlap():
    # windows [8,11) and [10,13) merge into one [8,13) cut; the suppressed
    # piece splits at both window boundaries and the overlap names both
    events = minute_series(100, lambda m: 90 if 6 <= m <= 20 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 8, 11, "A"), mw(2, "d", 10, 13, "B")],
    )
    assert ivs(result.faults) == [(13 * 60, 21 * 60)]
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (8 * 60, 10 * 60, [1]),
        (10 * 60, 11 * 60, [1, 2]),
        (11 * 60, 13 * 60, [2]),
    ]


def test_window_touching_piece_at_a_point_is_not_attributed():
    # window B starts exactly where the suppressed piece ends: a zero-length
    # contact neither splits the piece nor appears in its attribution
    events = minute_series(100, lambda m: 90 if 6 <= m <= 11 else 10)  # held [6,12)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 10, 12, "A"), mw(2, "d", 12, 15, "B")],
    )
    # [6,10) survives the merged [10,15) cut but is shorter than the duration
    assert result.faults == []
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [(10 * 60, 12 * 60, [1])]


def test_contained_window_adds_shared_attribution_inside_its_span():
    # a window fully inside another one shares attribution only on its span
    events = minute_series(100, lambda m: 90 if 6 <= m <= 20 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 8, 14, "A"), mw(2, "d", 10, 12, "B")],
    )
    assert ivs(result.faults) == [(14 * 60, 21 * 60)]
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (8 * 60, 10 * 60, [1]),
        (10 * 60, 12 * 60, [1, 2]),
        (12 * 60, 14 * 60, [1]),
    ]


# --------------------------------------------------------------------------- #
# attribute_windows primitive
# --------------------------------------------------------------------------- #


def test_attribute_windows_touching_windows_switch_at_junction():
    assert attribute_windows(Interval(0, 20), [(1, 0.0, 10.0), (2, 10.0, 20.0)]) == [
        (Interval(0, 10), [1]),
        (Interval(10, 20), [2]),
    ]


def test_attribute_windows_overlapping_windows_share_common_stretch():
    assert attribute_windows(Interval(0, 20), [(1, 0.0, 12.0), (2, 8.0, 20.0)]) == [
        (Interval(0, 8), [1]),
        (Interval(8, 12), [1, 2]),
        (Interval(12, 20), [2]),
    ]


def test_attribute_windows_zero_length_contact_contributes_nothing():
    # windows ending exactly at the piece start or starting exactly at its
    # end neither split the piece nor are attributed
    assert attribute_windows(
        Interval(10, 20), [(1, 0.0, 10.0), (2, 10.0, 20.0), (3, 20.0, 30.0)]
    ) == [(Interval(10, 20), [2])]


def test_attribute_windows_identical_windows_share_whole_piece():
    assert attribute_windows(Interval(0, 10), [(2, 0.0, 10.0), (1, 0.0, 10.0)]) == [
        (Interval(0, 10), [1, 2])
    ]


def test_attribute_windows_drops_uncovered_stretches():
    # a piece reaching beyond every window emits only its covered pieces
    assert attribute_windows(Interval(0, 20), [(1, 3.0, 8.0)]) == [
        (Interval(3, 8), [1])
    ]
