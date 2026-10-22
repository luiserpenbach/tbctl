"""LjmDevice against a fake LJM module: calls are serialised on USB only."""

import sys
import threading
import time
import types

import pytest

from tbctl.hw.ljm_iface import LjmDevice

CT_USB, CT_ETHERNET = 1, 3


@pytest.fixture
def fake_ljm(monkeypatch):
    log: list[tuple[str, float]] = []
    ljm = types.SimpleNamespace(
        constants=types.SimpleNamespace(
            ctUSB=CT_USB,
            UINT32=1,
            STREAM_SCANS_RETURN="LJM_STREAM_SCANS_RETURN",
            STREAM_SCANS_RETURN_ALL_OR_NONE=2,
        ),
        errorcodes=types.SimpleNamespace(NO_SCANS_RETURNED=1309),
        LJMError=type("LJMError", (Exception,), {}),
        writeLibraryConfigS=lambda name, value: None,
        transport=CT_USB,
        openS=lambda *a: 7,
        close=lambda h: None,
        namesToAddresses=lambda n, names: (list(range(n)), [3] * n),
        eWriteName=lambda h, name, v: None,
        eStreamStart=lambda h, spr, n, addrs, rate: rate,
    )
    ljm.getHandleInfo = lambda h: (7, ljm.transport, 1, 0, 0, 0)

    def stream_read(h):
        log.append(("stream begin", time.monotonic()))
        time.sleep(0.2)  # blocks until the block is complete
        log.append(("stream end", time.monotonic()))
        return [0.0] * 4, 0, 0

    def read_name(h, name):
        log.append(("read", time.monotonic()))
        return 1.0

    ljm.eStreamRead = stream_read
    ljm.eReadName = read_name
    ljm.log = log
    monkeypatch.setitem(sys.modules, "labjack", types.SimpleNamespace(ljm=ljm))
    return ljm


def _read_during_stream(dev: LjmDevice) -> None:
    dev.stream_start(["AIN0", "AIN1"], 100.0, 2)
    t = threading.Thread(target=dev.stream_read)
    t.start()
    time.sleep(0.05)  # the stream read is now blocked inside LJM
    dev.read_name("TEMPERATURE_DEVICE_K")
    t.join()


def test_usb_serialises_reads_behind_the_stream_read(fake_ljm):
    dev = LjmDevice("ANY", "USB")
    dev.open()
    assert dev.serialised
    _read_during_stream(dev)
    order = [name for name, _ in fake_ljm.log]
    assert order == ["stream begin", "stream end", "read"]


def test_ethernet_reads_do_not_wait_for_the_stream_read(fake_ljm):
    fake_ljm.transport = CT_ETHERNET
    dev = LjmDevice("192.168.10.20", "ETHERNET")
    dev.open()
    assert not dev.serialised
    _read_during_stream(dev)
    order = [name for name, _ in fake_ljm.log]
    assert order == ["stream begin", "read", "stream end"]


def test_unknown_transport_is_serialised(fake_ljm):
    def broken(h):
        raise RuntimeError("no handle info")

    fake_ljm.getHandleInfo = broken
    dev = LjmDevice("ANY", "ANY")
    dev.open()
    assert dev.serialised
