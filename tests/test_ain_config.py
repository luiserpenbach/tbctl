"""AIN range + single-ended/differential configuration (written to the T7
before every stream start) and its validation."""

import pytest
from pydantic import ValidationError

from tbctl.config.loader import load_channels
from tbctl.config.models import ChannelMap
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services.acquisition import AcquisitionService
from tests.conftest import CONFIGS


def _chmap(*channels) -> ChannelMap:
    return ChannelMap.model_validate(
        {
            "device": {"ip": "192.168.10.20", "watchdog_timeout_s": 5},
            "rate_groups": {"default": {"rate_hz": 50}},
            "channels": list(channels),
        }
    )


AIN = {"tag": "pressure.a", "hw": "AIN0", "kind": "analog_in", "unit": "bar"}


def test_example_settings():
    regs = dict(load_channels(CONFIGS / "channels.yaml").model.ain_settings())
    assert regs["AIN0_RANGE"] == 10.0 and regs["AIN0_NEGATIVE_CH"] == 199  # single-ended
    assert regs["AIN6_RANGE"] == 0.1 and regs["AIN6_NEGATIVE_CH"] == 7  # load cell bridge
    # thermocouples default to +-0.1 V; Mux80 pair n / n+8
    assert regs["AIN48_RANGE"] == 0.1 and regs["AIN48_NEGATIVE_CH"] == 56


async def test_written_before_stream_start():
    chmap = load_channels(CONFIGS / "channels.yaml").model
    mock = MockDevice(chmap)
    mock.open()
    acq = AcquisitionService(chmap, mock)
    await acq.start()
    try:
        for name, value in chmap.ain_settings():
            assert mock.read_name(name) == value
    finally:
        await acq.stop()


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"range": 5.0}, "range must be one of"),
        ({"negative_ch": "AIN2"}, "even/odd neighbours"),
        ({"hw": "AIN1", "negative_ch": "AIN2"}, "even/odd neighbours"),
        ({"hw": "AIN48", "negative_ch": "AIN49"}, r"AINn/AIN\(n\+8\)"),
        ({"hw": "AIN56", "negative_ch": "AIN64"}, r"AINn/AIN\(n\+8\)"),
        ({"hw": "AIN14", "negative_ch": "AIN15"}, "cannot be differential"),
        ({"negative_ch": "FIO1"}, "must be an AIN"),
    ],
)
def test_bad_settings_rejected(extra, match):
    with pytest.raises(ValidationError, match=match):
        _chmap({**AIN, **extra})


def test_negative_input_cannot_be_used_twice():
    with pytest.raises(ValidationError, match="differential negative input"):
        _chmap({**AIN, "negative_ch": "AIN1"}, {**AIN, "tag": "pressure.b", "hw": "AIN1"})
    ok = _chmap({**AIN, "negative_ch": "AIN1"}, {**AIN, "tag": "pressure.b", "hw": "AIN2"})
    assert dict(ok.ain_settings())["AIN2_NEGATIVE_CH"] == 199
