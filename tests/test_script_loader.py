"""sequencer.lua deploy: the flash copy must survive a power cycle (TODO.md §3 test 5)."""

import pytest

from tbctl.hw import script_loader
from tbctl.hw.registers_gen import INTERP_VERSION, REG_NAME
from tbctl.hw.script_loader import ScriptLoadError, deploy_script

SOURCE = "MB.W(46018,3,1) while true do end\n"


class LuaT7:
    """The T7 Lua registers as the 2026-10-22 bench saw them: LUA_RUN compiles
    and frees the source buffer, LUA_SAVE_TO_FLASH saves whatever is loaded
    (an empty buffer too, without an error) padded by 8 bytes, and at power-up
    LUA_RUN_DEFAULT=1 runs the flash copy."""

    def __init__(self, *, save_truncates: bool = False):
        self.regs: dict[str, float] = {}
        self.buffer = b""
        self.flash = b""
        self.running = False
        self.save_truncates = save_truncates

    def read_name(self, name: str) -> float:
        if name == "LUA_RUN":
            return 1 if self.running else 0
        if name == "LUA_SOURCE_SIZE":
            return len(self.buffer)
        return self.regs.get(name, 0)

    def write_name(self, name: str, value: float) -> None:
        if name == "LUA_RUN":
            if value:
                self._run()
            else:
                self.running = False
        elif name == "LUA_SOURCE_SIZE":
            self.buffer = b""
        elif name == "LUA_SAVE_TO_FLASH":
            kept = self.buffer[: len(self.buffer) // 2] if self.save_truncates else self.buffer
            self.flash = kept + bytes(8) if kept else b""
        elif name == "LUA_LOAD_SAVED":
            self.buffer = self.flash
        else:
            self.regs[name] = value

    def write_name_bytes(self, name: str, data: bytes) -> None:
        assert name == "LUA_SOURCE_WRITE"
        self.buffer = data

    def stream_stop(self) -> None:
        pass

    def _run(self) -> None:
        if not self.buffer:
            raise RuntimeError("LUA_CODE_BUFFER_EMPTY")
        self.running = True
        # the script publishes interp_version once it starts
        self.regs[REG_NAME["INTERP_VERSION"]] = INTERP_VERSION
        self.buffer = b""

    def power_cycle(self) -> None:
        self.running = False
        self.buffer = b""
        self.regs = {k: v for k, v in self.regs.items() if k.endswith("_DEFAULT")}
        if self.regs.get("LUA_RUN_DEFAULT") and self.flash:
            self.buffer = self.flash
            self._run()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(script_loader.time, "sleep", lambda _s: None)


def test_deployed_script_autostarts_after_power_cycle():
    dev = LuaT7()
    assert deploy_script(dev, SOURCE) == INTERP_VERSION
    assert dev.running
    dev.power_cycle()
    assert dev.running
    assert dev.read_name(REG_NAME["INTERP_VERSION"]) == INTERP_VERSION


def test_ram_only_deploy_leaves_flash_and_autostart_alone():
    dev = LuaT7()
    deploy_script(dev, SOURCE, save_to_flash=False)
    assert dev.running and dev.flash == b""
    assert dev.read_name("LUA_RUN_DEFAULT") == 0


def test_bad_flash_copy_is_refused():
    dev = LuaT7(save_truncates=True)
    with pytest.raises(ScriptLoadError, match="flash holds"):
        deploy_script(dev, SOURCE)
    assert dev.read_name("LUA_RUN_DEFAULT") == 0
