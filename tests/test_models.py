"""Schema validation matrix (spec §10.1) for the §4 config models."""

import pytest
from pydantic import ValidationError

from tbctl.config.models import (
    ChannelMap,
    Dashboard,
    Sequence,
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
    chmap(valves)  # exactly 16 is fine
    with pytest.raises(ValidationError):
        chmap([*valves, {"tag": "valve.v16", "hw": "CIO0", "kind": "valve", "fail_safe": "closed"}])


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
    m = chmap([{**VALVE, "hw": "EIO2"}, SPARK])
    assert m.bit_index("valve.ox_main") == 10
    assert m.bit_index("spark.igniter") == 6
    assert [c.tag for c in m.outputs()] == ["spark.igniter", "valve.ox_main"]


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


def test_precondition_needs_bound():
    with pytest.raises(ValidationError, match="min and/or max"):
        seq([{"t": 0.0, "set": {"a.b": "open"}}], preconditions=[{"tag": "pressure.x"}])


def test_redlines_key_reserved():
    s = seq([{"t": 0.0, "set": {"a.b": "open"}}], redlines=[{"whatever": 1}])
    assert s.redlines == [{"whatever": 1}]


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
