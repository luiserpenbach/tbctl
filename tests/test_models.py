"""Schema validation matrix (spec §10.1) for the §4 config models."""

import pytest
from pydantic import ValidationError

from tbctl.config.models import (
    ChannelMap,
    Dashboard,
    ReviewConfig,
    Sequence,
    cross_validate_dashboard,
    cross_validate_review,
    cross_validate_sequence,
    dio_number,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DEVICE = {"ip": "192.168.10.20", "watchdog_timeout_s": 5}
RATE_GROUPS = {"default": {"rate_hz": 50}}


def chmap(channels: list[dict], **overrides) -> ChannelMap:
    data = {"device": DEVICE, "rate_groups": RATE_GROUPS, "channels": channels}
    data.update(overrides)
    return ChannelMap.model_validate(data)


AIN = {"tag": "pressure.chamber", "hw": "AIN0", "kind": "analog_in", "unit": "bar"}
VALVE = {"tag": "valve.ox_main", "hw": "FIO0", "kind": "valve", "fail_safe": "closed"}
SPARK = {"tag": "spark.igniter", "hw": "FIO6", "kind": "spark", "fail_safe": "off"}


# ---------------------------------------------------------------------------
# channel map
# ---------------------------------------------------------------------------


def test_minimal_valid_map():
    m = chmap([AIN, VALVE, SPARK])
    assert m.by_tag("valve.ox_main").hw == "FIO0"


def test_device_connection_modes():
    m = chmap([AIN])
    assert m.device.connection == "ethernet"
    assert m.device.ljm_open_args() == ("ETHERNET", "192.168.10.20")
    usb = ChannelMap.model_validate(
        {
            "device": {**DEVICE, "connection": "usb"},
            "rate_groups": RATE_GROUPS,
            "channels": [AIN],
        }
    )
    assert usb.device.ljm_open_args() == ("USB", "ANY")
    with pytest.raises(ValidationError):
        chmap([AIN], device={**DEVICE, "connection": "wifi"})


def test_dio_numbering():
    assert dio_number("FIO0") == 0
    assert dio_number("FIO7") == 7
    assert dio_number("EIO0") == 8
    assert dio_number("EIO7") == 15
    assert dio_number("CIO0") == 16
    with pytest.raises(ValueError):
        dio_number("FIO8")
    with pytest.raises(ValueError):
        dio_number("AIN0")


def test_duplicate_tags_rejected():
    with pytest.raises(ValidationError, match="duplicate channel tags"):
        chmap([VALVE, {**VALVE, "hw": "FIO1"}])


def test_duplicate_hw_rejected():
    with pytest.raises(ValidationError, match="duplicate hw"):
        chmap([VALVE, {**VALVE, "tag": "valve.other"}])


def test_valve_requires_fail_safe():
    bad = {k: v for k, v in VALVE.items() if k != "fail_safe"}
    with pytest.raises(ValidationError):
        chmap([bad])


def test_output_on_cio_rejected():
    with pytest.raises(ValidationError, match="FIO0-7/EIO0-7"):
        chmap([{**VALVE, "hw": "CIO0"}])


def test_more_than_16_outputs_rejected():
    valves = [
        {"tag": f"valve.v{i}", "hw": hw, "kind": "valve", "fail_safe": "closed"}
        for i, hw in enumerate([f"FIO{j}" for j in range(8)] + [f"EIO{j}" for j in range(8)])
    ]
    chmap([AIN, *valves])  # exactly 16 is fine
    with pytest.raises(ValidationError):
        chmap(
            [
                AIN,
                *valves,
                {"tag": "valve.v16", "hw": "CIO0", "kind": "valve", "fail_safe": "closed"},
            ]
        )


def test_unknown_rate_group_rejected():
    with pytest.raises(ValidationError, match="rate_group"):
        chmap([{**AIN, "rate_group": "nope"}])


def test_missing_default_rate_group_rejected():
    with pytest.raises(ValidationError, match="default"):
        chmap([AIN], rate_groups={"slow": {"rate_hz": 1}})


def test_bad_tag_format_rejected():
    with pytest.raises(ValidationError, match="dotted lowercase"):
        chmap([{**AIN, "tag": "Chamber Pressure"}])


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        chmap([{**AIN, "banana": 1}])


def test_cal_table_monotonic():
    with pytest.raises(ValidationError, match="strictly increasing"):
        chmap([{**AIN, "cal": {"kind": "table", "x": [0, 0], "y": [1, 2]}}])


def test_safe_word_and_polarity():
    # NO valve, energize-to-open, fail closed  -> safe level 0
    # NC vent, open when de-energized (open_state low), fail open -> safe level 0
    # NC vent that fails open with open_state high would be safe level 1
    m = chmap(
        [
            AIN,  # maps need >=1 input; AINs have no DIO bit
            VALVE,  # FIO0, fail closed, open high -> safe 0
            {
                "tag": "valve.vent",
                "hw": "FIO1",
                "kind": "valve",
                "fail_safe": "open",
                "open_state": "low",
            },  # open when low -> fail-safe open = level 0
            {
                "tag": "valve.odd",
                "hw": "FIO2",
                "kind": "valve",
                "fail_safe": "closed",
                "open_state": "low",
            },  # closed = level 1
            SPARK,  # FIO6, off = level 0
        ]
    )
    assert m.safe_word() == 0b0000_0100  # only valve.odd sits high when safe
    assert m.controlled_mask() == 0b0100_0111
    assert m.dio_inhibit() == 0xFFFF & ~0b0100_0111


def test_bit_index_by_dio_number():
    m = chmap([AIN, {**VALVE, "hw": "EIO2"}, SPARK])
    assert m.bit_index("valve.ox_main") == 10
    assert m.bit_index("spark.igniter") == 6
    assert [c.tag for c in m.outputs()] == ["spark.igniter", "valve.ox_main"]


def test_at_least_one_input_channel_required():
    """C5/L15: output-only maps would fail at eStreamStart, not validation."""
    with pytest.raises(ValidationError, match="at least one input channel"):
        chmap([VALVE, SPARK])


def test_rate_group_divisibility():
    """C5: mirror of AcquisitionService._configure — a used group whose rate
    does not integer-divide the stream (fastest used) rate must not validate,
    or hot-apply/startup crashes after the file is already persisted."""
    with pytest.raises(ValidationError, match="must divide stream rate"):
        chmap(
            [AIN, {**AIN, "tag": "pressure.odd", "hw": "AIN1", "rate_group": "odd"}],
            rate_groups={"default": {"rate_hz": 50}, "odd": {"rate_hz": 7}},
        )
    # exactly the report's reproducer: an unused-but-declared odd group is
    # fine only if no channel lands in it...
    m = chmap([AIN], rate_groups={"default": {"rate_hz": 50}, "odd": {"rate_hz": 7}})
    assert m.rate_groups["odd"].rate_hz == 7
    # ...and integer decimation passes
    m = chmap(
        [AIN, {**AIN, "tag": "pressure.fast", "hw": "AIN1", "rate_group": "fast"}],
        rate_groups={"default": {"rate_hz": 50}, "fast": {"rate_hz": 1000}},
    )
    assert m.rate_groups["fast"].rate_hz == 1000


def test_thermocouple_ain_capped():
    """M21: AIN9999 must not pass validation and fail at stream start."""
    tc = {"tag": "temp.t1", "hw": "AIN48", "kind": "thermocouple_k"}
    chmap([{**AIN}, tc])
    with pytest.raises(ValidationError, match=r"AIN0\.\.AIN127"):
        chmap([AIN, {**tc, "hw": "AIN9999"}])
    with pytest.raises(ValidationError, match=r"AIN0\.\.AIN127"):
        chmap([AIN, {**tc, "hw": "AIN128"}])


def test_analog_range_constrained_to_ljm_set():
    """L17: LJM silently coerces anything outside {10, 1, 0.1, 0.01} V."""
    for ok in (10.0, 1.0, 0.1, 0.01):
        chmap([{**AIN, "range": ok}])
    for bad in (5.0, 0.2, 0.0, -10.0, 100.0):
        with pytest.raises(ValidationError, match="range must be one of"):
            chmap([{**AIN, "range": bad}])


# ---------------------------------------------------------------------------
# sequences
# ---------------------------------------------------------------------------


def seq(steps, abort=None, **overrides) -> Sequence:
    data = {
        "name": "test_seq",
        "max_duration_s": 20.0,
        "steps": steps,
        "abort": abort or [{"t": 0.0, "set": {"valve.ox_main": "close"}}],
    }
    data.update(overrides)
    return Sequence.model_validate(data)


def test_valid_sequence():
    s = seq(
        [
            {"t": 0.0, "set": {"valve.ox_main": "open"}},
            {"t": 1.0, "set": {"valve.ox_main": "close"}},
        ]
    )
    assert len(s.steps) == 2


def test_non_monotonic_times_rejected():
    with pytest.raises(ValidationError, match=r"non-decreasing|closer"):
        seq([{"t": 1.0, "set": {"a.b": "open"}}, {"t": 0.5, "set": {"a.b": "close"}}])


def test_min_spacing_2ms():
    with pytest.raises(ValidationError, match="closer than 2 ms"):
        seq([{"t": 0.0, "set": {"a.b": "open"}}, {"t": 0.001, "set": {"a.b": "close"}}])
    seq([{"t": 0.0, "set": {"a.b": "open"}}, {"t": 0.002, "set": {"a.b": "close"}}])


def test_max_128_steps():
    steps = [{"t": i * 0.01, "set": {"a.b": "open"}} for i in range(129)]
    with pytest.raises(ValidationError, match="exceeds max 128"):
        seq(steps)


def test_duration_cap():
    with pytest.raises(ValidationError, match="max_duration_s"):
        seq([{"t": 25.0, "set": {"a.b": "open"}}])


def test_abort_required():
    with pytest.raises(ValidationError):
        Sequence.model_validate(
            {
                "name": "x",
                "max_duration_s": 5.0,
                "steps": [{"t": 0, "set": {"a.b": "open"}}],
                "abort": [],
            }
        )


def test_abort_must_start_at_t_zero():
    """M12: any positive first abort time is added latency with the engine
    still in its firing state."""
    with pytest.raises(ValidationError, match="abort: first step must be at t=0"):
        seq(
            [{"t": 0.0, "set": {"a.b": "open"}}],
            abort=[{"t": 0.5, "set": {"a.b": "close"}}],
        )
    seq(  # t=0 first step is fine
        [{"t": 0.0, "set": {"a.b": "open"}}],
        abort=[{"t": 0.0, "set": {"a.b": "close"}}, {"t": 1.0, "set": {"a.b": "close"}}],
    )


def test_step_time_one_hour_cap():
    """M16: the Lua rejects t > 3600 s (BAD_TABLE); mirror it host-side on
    BOTH tables so it can never surface as a device fault or a 500."""
    # abort table (previously completely unbounded)
    with pytest.raises(ValidationError, match="1 h"):
        seq(
            [{"t": 0.0, "set": {"a.b": "open"}}],
            abort=[{"t": 0.0, "set": {"a.b": "close"}}, {"t": 5e6, "set": {"a.b": "close"}}],
        )
    with pytest.raises(ValidationError, match="1 h"):
        seq(
            [{"t": 0.0, "set": {"a.b": "open"}}],
            abort=[{"t": 0.0, "set": {"a.b": "close"}}, {"t": 3601.0, "set": {"a.b": "close"}}],
        )
    # main table: max_duration_s (<=600) already binds tighter, but the cap
    # must hold even without it
    with pytest.raises(ValidationError):
        seq([{"t": 4000.0, "set": {"a.b": "open"}}])
    # exactly 3600 s in the abort table is allowed
    seq(
        [{"t": 0.0, "set": {"a.b": "open"}}],
        abort=[{"t": 0.0, "set": {"a.b": "close"}}, {"t": 3600.0, "set": {"a.b": "close"}}],
    )


def test_wire_limits_come_from_generated_constants():
    """F2: models must not hardcode the wire-format limits."""
    from tbctl.config import models
    from tbctl.hw import registers_gen

    assert models.MAX_STEPS is registers_gen.MAX_STEPS
    assert models.MIN_STEP_SPACING_S == registers_gen.MIN_STEP_SPACING_MS / 1000.0


def test_precondition_needs_bound():
    with pytest.raises(ValidationError, match="min and/or max"):
        seq([{"t": 0.0, "set": {"a.b": "open"}}], preconditions=[{"tag": "pressure.x"}])


def test_redlines_typed():
    s = seq(
        [{"t": 0.0, "set": {"a.b": "open"}}],
        redlines=[{"tag": "pressure.chamber", "max": 22.0}],
    )
    assert s.redlines[0].max == 22.0 and s.redlines[0].persist_ms == 50
    with pytest.raises(ValidationError):
        seq([{"t": 0.0, "set": {"a.b": "open"}}], redlines=[{"whatever": 1}])


def test_cross_validation():
    m = chmap([AIN, VALVE, SPARK])
    good = seq(
        [{"t": 0.0, "set": {"valve.ox_main": "open", "spark.igniter": "on"}}],
        preconditions=[{"tag": "pressure.chamber", "min": 1.0}],
    )
    assert cross_validate_sequence(good, m) == []

    bad = seq(
        [
            {
                "t": 0.0,
                "set": {"valve.nope": "open", "pressure.chamber": "open", "spark.igniter": "open"},
            }
        ],
        preconditions=[{"tag": "valve.ox_main", "min": 1.0}],
    )
    errors = cross_validate_sequence(bad, m)
    assert len(errors) == 4  # unknown tag, input as output, bad spark value, output as precondition


# --------------------------------------------------- TableCal limit sanity


TABLE_CAL_AIN = {
    **AIN,
    "cal": {"kind": "table", "x": [0.0, 5.0], "y": [0.0, 20.0]},  # clamps to 0..20
}


def test_redline_outside_tablecal_range_rejected():
    """M19: np.interp clamps to the table's y-range, so a limit at/outside
    that range is a redline that can physically never trip."""
    m = chmap([TABLE_CAL_AIN, VALVE])
    steps = [
        {"t": 0.0, "set": {"valve.ox_main": "open"}},
        {"t": 1.0, "set": {"valve.ox_main": "close"}},
    ]

    # the report's exact case: cal tops out at 20, redline max 22 is dead
    dead = seq(steps, redlines=[{"tag": "pressure.chamber", "max": 22.0}])
    errors = cross_validate_sequence(dead, m)
    assert len(errors) == 1 and "representable range" in errors[0]

    # max exactly at the clamp ceiling can also never trip (v > 20 impossible)
    assert cross_validate_sequence(
        seq(steps, redlines=[{"tag": "pressure.chamber", "max": 20.0}]), m
    )
    # min at/below the clamp floor is equally dead
    assert cross_validate_sequence(
        seq(steps, redlines=[{"tag": "pressure.chamber", "min": 0.0}]), m
    )
    assert cross_validate_sequence(
        seq(steps, redlines=[{"tag": "pressure.chamber", "min": -5.0}]), m
    )
    # in-range limits are fine
    assert (
        cross_validate_sequence(
            seq(steps, redlines=[{"tag": "pressure.chamber", "min": 0.5, "max": 18.0}]), m
        )
        == []
    )
    # non-table cals are not range-checked
    m_lin = chmap([AIN, VALVE])
    assert (
        cross_validate_sequence(
            seq(steps, redlines=[{"tag": "pressure.chamber", "max": 1e9}]), m_lin
        )
        == []
    )


def test_precondition_outside_tablecal_range_rejected():
    m = chmap([TABLE_CAL_AIN, VALVE])
    steps = [
        {"t": 0.0, "set": {"valve.ox_main": "open"}},
        {"t": 1.0, "set": {"valve.ox_main": "close"}},
    ]
    errors = cross_validate_sequence(
        seq(steps, preconditions=[{"tag": "pressure.chamber", "min": 25.0}]), m
    )
    assert len(errors) == 1 and "preconditions[0]" in errors[0]
    assert (
        cross_validate_sequence(
            seq(steps, preconditions=[{"tag": "pressure.chamber", "min": 5.0}]), m
        )
        == []
    )


# ---------------------------------------------------------------------------
# review rules
# ---------------------------------------------------------------------------


def test_review_sequence_scoping_validated():
    """M20: a rule scoped to a renamed/nonexistent sequence silently never
    runs; with the sequence set known it must be an error."""
    m = chmap([AIN, VALVE])
    cfg = ReviewConfig.model_validate(
        {
            "rules": [
                {
                    "name": "pc_peak",
                    "tag": "pressure.chamber",
                    "metric": "max",
                    "limit_max": 25.0,
                    "sequences": ["hotfire_v1"],
                }
            ]
        }
    )
    assert cross_validate_review(cfg, m, sequence_names={"hotfire_v1", "coldflow_v1"}) == []
    errors = cross_validate_review(cfg, m, sequence_names={"coldflow_v1"})
    assert len(errors) == 1 and "unknown sequence 'hotfire_v1'" in errors[0]
    # without the sequence set (channel-only callers) scoping is not checked
    assert cross_validate_review(cfg, m) == []


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------


def test_dashboard_valid():
    d = Dashboard.model_validate(
        {
            "name": "d",
            "grid": {"cols": 12, "row_height_px": 40},
            "panels": [{"id": "p1", "type": "timeseries", "x": 0, "y": 0, "w": 8, "h": 8}],
        }
    )
    assert d.panels[0].type == "timeseries"


def test_dashboard_duplicate_ids():
    with pytest.raises(ValidationError, match="duplicate panel ids"):
        Dashboard.model_validate(
            {
                "name": "d",
                "panels": [
                    {"id": "p1", "type": "timeseries", "x": 0, "y": 0, "w": 4, "h": 4},
                    {"id": "p1", "type": "valve_panel", "x": 4, "y": 0, "w": 4, "h": 4},
                ],
            }
        )


def test_dashboard_panel_overflow():
    with pytest.raises(ValidationError, match="exceeds grid cols"):
        Dashboard.model_validate(
            {
                "name": "d",
                "panels": [{"id": "p1", "type": "timeseries", "x": 8, "y": 0, "w": 8, "h": 4}],
            }
        )


def test_manual_y_axis_needs_bounds():
    with pytest.raises(ValidationError, match="min and max"):
        Dashboard.model_validate(
            {
                "name": "d",
                "panels": [
                    {
                        "id": "p1",
                        "type": "timeseries",
                        "x": 0,
                        "y": 0,
                        "w": 4,
                        "h": 4,
                        "y_axis": {"mode": "manual"},
                    }
                ],
            }
        )


def _panel(**kw) -> dict:
    return {"id": "p1", "type": "timeseries", "x": 0, "y": 0, "w": 4, "h": 4, **kw}


def test_manual_y_axis_min_lt_max():
    """L17: manual y-axis with min >= max blanks the chart."""
    with pytest.raises(ValidationError, match="min < max"):
        Dashboard.model_validate(
            {"name": "d", "panels": [_panel(y_axis={"mode": "manual", "min": 5.0, "max": 5.0})]}
        )
    Dashboard.model_validate(
        {"name": "d", "panels": [_panel(y_axis={"mode": "manual", "min": 0.0, "max": 5.0})]}
    )


def test_pid_sensor_warn_bounds_ordered():
    """L17: warn_min >= warn_max is a nonsensical warning band."""
    from tbctl.config.models import PidConfig

    el = {"id": "s1", "type": "sensor", "channel": "pressure.chamber", "x": 0.5, "y": 0.5}
    with pytest.raises(ValidationError, match="warn_min must be < warn_max"):
        PidConfig.model_validate(
            {"name": "p", "elements": [{**el, "warn_min": 10.0, "warn_max": 1.0}]}
        )
    PidConfig.model_validate({"name": "p", "elements": [{**el, "warn_min": 1.0, "warn_max": 10.0}]})


def test_dashboard_cross_validation():
    """L17: literal panel tags must exist in the channel map; `pid` panels
    must reference an existing P&ID config; glob patterns are skipped."""
    m = chmap([AIN, VALVE, SPARK])
    d = Dashboard.model_validate(
        {
            "name": "d",
            "panels": [
                _panel(channels=["pressure.chamber", "valve.ox_main"]),
                {**_panel(), "id": "p2", "type": "value_table", "channels": ["pressure.*"]},
            ],
        }
    )
    assert cross_validate_dashboard(d, m) == []

    bad = Dashboard.model_validate({"name": "d", "panels": [_panel(channels=["pressure.nope"])]})
    errors = cross_validate_dashboard(bad, m)
    assert len(errors) == 1 and "pressure.nope" in errors[0]

    pid_panel = Dashboard.model_validate(
        {"name": "d", "panels": [{**_panel(), "type": "pid", "pid": "feed"}]}
    )
    assert cross_validate_dashboard(pid_panel, m, pid_names={"feed"}) == []
    errors = cross_validate_dashboard(pid_panel, m, pid_names={"other"})
    assert len(errors) == 1 and "unknown pid config 'feed'" in errors[0]

    no_pid = Dashboard.model_validate({"name": "d", "panels": [{**_panel(), "type": "pid"}]})
    errors = cross_validate_dashboard(no_pid, m, pid_names=set())
    assert len(errors) == 1 and "needs a `pid:` config name" in errors[0]
