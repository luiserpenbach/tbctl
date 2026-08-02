"""Generate Python constants and the Lua header block from shared/registers.yaml.

This is the ONLY place where symbolic register names are resolved to numeric
Modbus addresses (spec D10). Resolution uses LJM's ljm_constants.json:

  1. $LJM_CONSTANTS_FILE if set
  2. the platform LJM install (/usr/local/share/LabJack/LJM/ljm_constants.json)
  3. the vendored copy in shared/vendor/ljm_constants.json (pinned)

Outputs (checked into the repo; CI verifies they are in sync):
  - src/tbctl/hw/registers_gen.py
  - lua/gen/registers_gen.lua

Run via `tbctl codegen` or `python shared/codegen.py`.
"""

from __future__ import annotations

import datetime
import itertools
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTERS_YAML = REPO_ROOT / "shared" / "registers.yaml"
VENDORED_CONSTANTS = REPO_ROOT / "shared" / "vendor" / "ljm_constants.json"
PY_OUT = REPO_ROOT / "src" / "tbctl" / "hw" / "registers_gen.py"
LUA_OUT = REPO_ROOT / "lua" / "gen" / "registers_gen.lua"
SEQUENCER_OUT = REPO_ROOT / "lua" / "sequencer.lua"

LJM_SEARCH_PATHS = [
    "/usr/local/share/LabJack/LJM/ljm_constants.json",
    "C:/ProgramData/LabJack/LJM/ljm_constants.json",
]

# LJM data type -> register width in 16-bit Modbus words
TYPE_WORDS = {"UINT16": 1, "UINT32": 2, "INT32": 2, "FLOAT32": 2}

_HASH_RANGE = re.compile(r"#\((\d+):(\d+)\)")


def find_constants_file() -> Path:
    env = os.environ.get("LJM_CONSTANTS_FILE")
    if env:
        p = Path(env)
        if not p.is_file():
            raise FileNotFoundError(f"LJM_CONSTANTS_FILE={env} does not exist")
        return p
    for cand in LJM_SEARCH_PATHS:
        if Path(cand).is_file():
            return Path(cand)
    return VENDORED_CONSTANTS


@dataclass(frozen=True)
class Register:
    name: str
    address: int
    type: str

    @property
    def words(self) -> int:
        return TYPE_WORDS[self.type]


class LjmConstants:
    """Index of ljm_constants.json with #(a:b) range expansion."""

    def __init__(self, path: Path):
        self.path = path
        raw = json.loads(path.read_text())
        self.version: str = raw["header"]["version"]
        self._exact: dict[str, Register] = {}
        self._ranged: list[dict] = []
        for r in raw["registers"]:
            name, addr, typ = r["name"], int(r["address"]), r["type"]
            m = _HASH_RANGE.search(name)
            if m:
                self._ranged.append(
                    {
                        "prefix": name[: m.start()],
                        "suffix": name[m.end() :],
                        "lo": int(m.group(1)),
                        "hi": int(m.group(2)),
                        "address": addr,
                        "type": typ,
                    }
                )
            else:
                self._exact[name] = Register(name, addr, typ)

    def resolve(self, name: str) -> Register:
        if name in self._exact:
            return self._exact[name]
        for r in self._ranged:
            if name.startswith(r["prefix"]) and name.endswith(r["suffix"]):
                mid = name[len(r["prefix"]) : len(name) - len(r["suffix"]) if r["suffix"] else None]
                if mid.isdigit():
                    idx = int(mid)
                    if r["lo"] <= idx <= r["hi"]:
                        width = TYPE_WORDS[r["type"]]
                        return Register(name, r["address"] + (idx - r["lo"]) * width, r["type"])
        raise KeyError(f"register {name!r} not found in {self.path}")


def load_spec() -> dict:
    return yaml.safe_load(REGISTERS_YAML.read_text())


def validate_spec(spec: dict) -> None:
    """Hard-fail codegen on spec edits the Lua side cannot honor (F1/F2/F4).

    Raises ValueError with an actionable message; called by generate() so a
    bad registers.yaml can never silently produce desynced artifacts.
    """
    c = spec["constants"]
    # F4: lua/sequencer_main.lua compares millisecond values (heartbeat age,
    # spark guard ms, the 3600000 ms step cap, the 250/1000-tick overrun and
    # clamp thresholds) DIRECTLY against tick counts. That arithmetic is only
    # correct at 1 ms per tick.
    if c["tick_ms"] != 1:
        raise ValueError(
            f"constants.tick_ms={c['tick_ms']}: lua/sequencer_main.lua compares millisecond "
            "limits directly to tick counts, which is only correct when tick_ms == 1. "
            "Changing the tick period requires rewriting the sequencer's timing arithmetic, "
            "not just this constant."
        )
    # F2: the FIFO must hold one maximal table upload:
    #   header + (max_steps main + max_steps abort) * step_bytes + CRC trailer.
    need = c["header_bytes"] + 2 * c["max_steps"] * c["step_bytes"] + c["trailer_bytes"]
    alloc = spec["fifo"]["table_upload"]["allocate_bytes"]
    if alloc < need:
        raise ValueError(
            f"fifo.table_upload.allocate_bytes={alloc} cannot hold a max-size table: "
            f"header {c['header_bytes']} + 2*{c['max_steps']}*{c['step_bytes']} step bytes "
            f"+ trailer {c['trailer_bytes']} = {need} B. Raise allocate_bytes or lower max_steps."
        )
    _check_lua_coverage(spec)


def resolve_all(spec: dict, ljm: LjmConstants) -> dict[str, Register]:
    """Resolve every symbolic slot + fifo register to a concrete Register."""
    resolved: dict[str, Register] = {}
    for direction in ("host_to_lua", "lua_to_host"):
        for logical, info in spec["slots"][direction].items():
            resolved[logical] = ljm.resolve(info["slot"])
    fifo = spec["fifo"]["table_upload"]
    resolved["FIFO_DATA"] = ljm.resolve(fifo["data_register"])
    resolved["FIFO_ALLOCATE"] = ljm.resolve(fifo["allocate_register"])
    resolved["FIFO_BYTES_IN"] = ljm.resolve(fifo["bytes_in_fifo_register"])
    resolved["FIFO_EMPTY"] = ljm.resolve(fifo["empty_register"])
    # Sanity: no two logical slots may overlap in address space.
    spans: list[tuple[int, int, str]] = []
    for logical, reg in resolved.items():
        if logical.startswith("FIFO_"):
            continue  # FIFO registers intentionally share fixed addresses
        spans.append((reg.address, reg.address + reg.words, logical))
    spans.sort()
    for (_a0, a1, n0), (b0, _b1, n1) in itertools.pairwise(spans):
        if b0 < a1:
            raise ValueError(f"register slot overlap: {n0} and {n1}")
    return resolved


def _stamp(ljm: LjmConstants) -> str:
    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"generated {ts} from registers.yaml + ljm_constants.json {ljm.version}"


def gen_python(spec: dict, resolved: dict[str, Register], ljm: LjmConstants) -> str:
    lines = [
        '"""AUTO-GENERATED by shared/codegen.py -- DO NOT EDIT.',
        "",
        f"{_stamp(ljm)}",
        '"""',
        "",
        "from enum import IntEnum",
        "",
        f"INTERP_VERSION = {spec['meta']['interp_version']}",
        f'LJM_CONSTANTS_VERSION = "{ljm.version}"',
        "",
    ]
    for const, val in spec["constants"].items():
        if const == "arm_magic":
            lines.append(f"ARM_MAGIC = 0x{val:04X}")
        else:
            lines.append(f"{const.upper()} = {val}")
    lines.append("")
    lines.append("# Logical slot name -> (LJM register name, Modbus address, type)")
    lines.append("REGISTERS: dict[str, tuple[str, int, str]] = {")
    for direction in ("host_to_lua", "lua_to_host"):
        lines.append(f"    # {direction}")
        for logical, _info in spec["slots"][direction].items():
            reg = resolved[logical]
            lines.append(f'    "{logical}": ("{reg.name}", {reg.address}, "{reg.type}"),')
    lines.append("    # fifo (table upload)")
    for logical in ("FIFO_DATA", "FIFO_ALLOCATE", "FIFO_BYTES_IN", "FIFO_EMPTY"):
        reg = resolved[logical]
        lines.append(f'    "{logical}": ("{reg.name}", {reg.address}, "{reg.type}"),')
    lines.append("}")
    lines.append("")
    lines.append("# Convenience: logical name -> LJM register name (for eWriteName et al.)")
    lines.append("REG_NAME: dict[str, str] = {k: v[0] for k, v in REGISTERS.items()}")
    lines.append("REG_ADDR: dict[str, int] = {k: v[1] for k, v in REGISTERS.items()}")
    lines.append("")
    fifo = spec["fifo"]["table_upload"]
    lines.append(f"FIFO_ALLOCATE_BYTES = {fifo['allocate_bytes']}")
    lines.append("")
    for enum_name, members in spec["enums"].items():
        cls = {"cmd": "Cmd", "state": "SeqState", "error": "SeqError"}[enum_name]
        lines.append(f"class {cls}(IntEnum):")
        for member, value in members.items():
            lines.append(f"    {member} = {value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Short keys for lua/sequencer_main.lua (must stay in sync with that file).
# Enums are inlined as numeric literals there to save heap; only A/K remain.
# The 1-char keys are load-bearing identifiers in sequencer_main.lua, so this
# map cannot be derived mechanically from registers.yaml -- instead
# _check_lua_coverage() fails codegen whenever the spec and these maps drift
# (F1): adding a slot or constant to registers.yaml without a Lua key here is
# a hard error, never a silent desync.
_LUA_ADDR_KEYS = {
    "CMD": "q",
    "ARM_CODE": "a",
    "HEARTBEAT": "h",
    "HB_TIMEOUT_MS": "t",
    "STATE": "s",
    "STEP_COUNT": "n",
    "CRC16": "c",
    "CUR_STEP": "u",
    "ERROR_CODE": "e",
    "INTERP_VERSION": "v",
    "ELAPSED_MS": "m",
    "FIFO_DATA": "f",
    "FIFO_ALLOCATE": "p",
    "FIFO_BYTES_IN": "b",
    "FIFO_EMPTY": "y",
    "DIO_STATE": "d",
    "DIO_INHIBIT": "i",
    "DIO_DIRECTION": "o",
}
_LUA_CONST_KEYS = {
    "INTERP_VERSION": "v",
    "ARM_MAGIC": "m",
    "DEFAULT_HB_TIMEOUT_MS": "h",
    "DEFAULT_SPARK_GUARD_MS": "g",
    "TICK_MS": "k",
    "MAX_STEPS": "s",
    "MIN_STEP_SPACING_MS": "p",
    "STEP_BYTES": "b",
    "HEADER_BYTES": "e",
    "TRAILER_BYTES": "r",
    "ALLOCATE_BYTES": "a",
}

# Fixed device registers the Lua writes directly; not registers.yaml slots.
_LUA_DIRECT_REGS = frozenset({"DIO_STATE", "DIO_INHIBIT", "DIO_DIRECTION"})
# Constants that come from spec sections other than `constants`.
_LUA_EXTRA_CONSTS = frozenset({"INTERP_VERSION", "ALLOCATE_BYTES"})


def _check_lua_coverage(spec: dict) -> None:
    """F1: every registers.yaml entry must have a Lua short key, and vice versa."""
    slot_names = {n for d in ("host_to_lua", "lua_to_host") for n in spec["slots"][d]}
    slot_names |= {"FIFO_DATA", "FIFO_ALLOCATE", "FIFO_BYTES_IN", "FIFO_EMPTY"}
    lua_slots = set(_LUA_ADDR_KEYS) - _LUA_DIRECT_REGS
    if missing := sorted(slot_names - lua_slots):
        raise ValueError(
            f"registers.yaml slots with no Lua counterpart in codegen._LUA_ADDR_KEYS: {missing}. "
            "Assign each a 1-char key there and use it from lua/sequencer_main.lua."
        )
    if stale := sorted(lua_slots - slot_names):
        raise ValueError(
            f"codegen._LUA_ADDR_KEYS entries no longer present in registers.yaml: {stale}"
        )
    const_names = set(_LUA_EXTRA_CONSTS) | {
        "ARM_MAGIC" if k == "arm_magic" else k.upper() for k in spec["constants"]
    }
    if missing := sorted(const_names - set(_LUA_CONST_KEYS)):
        raise ValueError(
            f"registers.yaml constants with no Lua counterpart in codegen._LUA_CONST_KEYS: "
            f"{missing}. Assign each a 1-char key there."
        )
    if stale := sorted(set(_LUA_CONST_KEYS) - const_names):
        raise ValueError(
            f"codegen._LUA_CONST_KEYS entries no longer present in registers.yaml: {stale}"
        )
    for label, mapping in (
        ("_LUA_ADDR_KEYS", _LUA_ADDR_KEYS),
        ("_LUA_CONST_KEYS", _LUA_CONST_KEYS),
    ):
        if len(set(mapping.values())) != len(mapping):
            raise ValueError(f"duplicate 1-char key in codegen.{label}")


def gen_lua(spec: dict, resolved: dict[str, Register], ljm: LjmConstants) -> str:
    # Everything hangs off one table `U` so nested functions close over a single
    # upvalue. T7 Lua caps locals at 50 and upvalues at 10. Keys are 1-char to
    # keep the assembled script under the T7 compile-size budget (~4 KiB).
    lines = [
        "-- AUTO-GENERATED by shared/codegen.py -- DO NOT EDIT.",
        f"-- {_stamp(ljm)}",
        "-- Compact U.A / U.K maps for lua/sequencer_main.lua (1-char keys).",
        "",
        "local U = {}",
        "U.A = {",
    ]
    for logical, key in _LUA_ADDR_KEYS.items():
        reg = ljm.resolve(logical) if logical in _LUA_DIRECT_REGS else resolved[logical]
        lines.append(f"  {key}={reg.address},")
    lines.append("}")
    consts = spec["constants"]
    const_vals = {
        "INTERP_VERSION": spec["meta"]["interp_version"],
        "ARM_MAGIC": consts["arm_magic"],
        "ALLOCATE_BYTES": spec["fifo"]["table_upload"]["allocate_bytes"],
        **{k.upper(): v for k, v in consts.items() if k != "arm_magic"},
    }
    lines.append("U.K = {")
    for logical, key in _LUA_CONST_KEYS.items():
        val = const_vals[logical]
        if logical == "ARM_MAGIC":
            lines.append(f"  {key}=0x{val:04X},")
        else:
            lines.append(f"  {key}={val},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines) + "\n"


def gen_sequencer(spec: dict, resolved: dict[str, Register], ljm: LjmConstants) -> str:
    """Assemble the deployable lua/sequencer.lua (header + logic).

    The CRC is implemented inside sequencer_main.lua as U.cb (incremental,
    per byte) so LOAD can stream the FIFO without a byte buffer — a
    1548-entry Lua byte table OOMs the T7's shared heap. lua/crc16.lua
    remains the host-side/desktop-harness reference implementation.
    """
    header = gen_lua(spec, resolved, ljm)
    main = (REPO_ROOT / "lua" / "sequencer_main.lua").read_text(encoding="utf-8")
    banner = (
        "-- ===========================================================================\n"
        f"-- tbctl sequencer.lua -- ASSEMBLED ARTIFACT, DO NOT EDIT DIRECTLY.\n"
        f"-- interp_version {spec['meta']['interp_version']} | {_stamp(ljm)}\n"
        "-- Edit lua/sequencer_main.lua (logic), lua/crc16.lua (CRC) or\n"
        "-- shared/registers.yaml (interface), then run `tbctl codegen`.\n"
        "-- DO NOT EDIT LOGIC WITHOUT BUMPING INTERP_VERSION AND RERUNNING sec 10.3.\n"
        "-- ===========================================================================\n\n"
    )
    return banner + header + "\n" + main


def generate(check: bool = False) -> int:
    ljm = LjmConstants(find_constants_file())
    spec = load_spec()
    validate_spec(spec)
    resolved = resolve_all(spec, ljm)
    outputs = {
        PY_OUT: gen_python(spec, resolved, ljm),
        LUA_OUT: gen_lua(spec, resolved, ljm),
        SEQUENCER_OUT: gen_sequencer(spec, resolved, ljm),
    }
    rc = 0
    for path, content in outputs.items():
        if check:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            # Ignore environment-dependent lines when comparing: the timestamp
            # and the LJM constants version (an installed ljm_constants.json is
            # preferred over the vendored copy, and its version varies per host;
            # the resolved ADDRESSES are still compared and must match).
            strip = lambda s: "\n".join(  # noqa: E731
                ln
                for ln in s.splitlines()
                if "generated 2" not in ln and "LJM_CONSTANTS_VERSION" not in ln
            )
            if strip(existing) != strip(content):
                print(f"OUT OF SYNC: {path}", file=sys.stderr)
                rc = 1
            else:
                print(f"ok: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {path} (LJM constants {ljm.version} from {ljm.path})")
    return rc


if __name__ == "__main__":
    sys.exit(generate(check="--check" in sys.argv))
