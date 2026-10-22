"""Listen addresses: the bench node binds loopback + the bench LAN only."""

from tbctl.node import _listen_socket, main
from tests.conftest import CONFIGS


def test_listen_socket_binds_the_given_address():
    sock = _listen_socket("127.0.0.1", 0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_unavailable_listen_address_exits_nonzero(tmp_path, capsys):
    # 192.0.2.0/24 is TEST-NET-1: never assigned here, like the bench address
    # before its cable is in. systemd restarts the node until it is up.
    argv = ["--sim", "--config-dir", str(CONFIGS), "--data-dir", str(tmp_path / "runs")]
    argv += ["--log-dir", str(tmp_path / "logs"), "--host", "127.0.0.1", "--host", "192.0.2.123"]
    assert main([*argv, "--port", "0"]) == 1
    assert "cannot listen on 192.0.2.123" in capsys.readouterr().err
