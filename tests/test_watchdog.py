"""Watchdog configuration + script loader against the mock register file."""

import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.hw.watchdog import configure_watchdog, disable_watchdog
from tests.conftest import CONFIGS


@pytest.fixture
def rig():
    loaded = load_channels(CONFIGS / "channels.yaml")
    mock = MockDevice(loaded.model)
    mock.open()
    return mock, loaded.model


def test_watchdog_settings(rig):
    mock, chmap = rig
    applied = configure_watchdog(mock, chmap)
    assert applied["WATCHDOG_ENABLE_DEFAULT"] == 1
    assert applied["WATCHDOG_TIMEOUT_S_DEFAULT"] == 5.0
    assert applied["WATCHDOG_DIO_STATE_DEFAULT"] == float(chmap.safe_word())
    # only controlled bits may be driven by the watchdog
    inhibit = int(applied["WATCHDOG_DIO_INHIBIT_DEFAULT"])
    assert inhibit & chmap.controlled_mask() == 0
    assert inhibit & 0xFFFF0000 == 0xFFFF0000
    assert mock.read_name("WATCHDOG_ENABLE_DEFAULT") == 1.0
    disable_watchdog(mock)
    assert mock.read_name("WATCHDOG_ENABLE_DEFAULT") == 0.0
