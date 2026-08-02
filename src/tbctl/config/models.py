"""Pydantic v2 models for all on-disk YAML configuration (spec §4).

Validation failure anywhere = refuse to start / refuse to load. Cross-file
checks (sequence tags vs. channel map) live in `cross_validate_sequence`;
full step-table compilation is the sequencer compiler's job (§4.2 rules that
need word expansion are enforced there).
"""

from __future__ import annotations

import itertools
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# DIO naming: global DIO numbers on the T7. The 16-bit sequence state word
# maps 1:1 onto DIO_STATE bits 0..15, so sequencer outputs must live on
# FIO0-7 (bits 0-7) or EIO0-7 (bits 8-15). "Bit index assigned
# deterministically by sorted DIO number" (§4.1) — the bit IS the DIO number,
# which keeps the Lua loop a single masked DIO_STATE write.
# ---------------------------------------------------------------------------
_DIO_BANKS = {"FIO": (0, 8), "EIO": (8, 8), "CIO": (16, 4), "MIO": (20, 3)}
_DIO_RE = re.compile(r"^(FIO|EIO|CIO|MIO)([0-9]+)$")
_AIN_RE = re.compile(r"^AIN([0-9]+)$")

MAX_OUTPUT_BITS = 16


def dio_number(hw: str) -> int:
    """Global DIO number for a DIO name like FIO3 / EIO5 (FIO0=0 ... MIO2=22)."""
    m = _DIO_RE.match(hw)
    if not m:
        raise ValueError(f"{hw!r} is not a DIO name (FIO*/EIO*/CIO*/MIO*)")
    bank, idx = m.group(1), int(m.group(2))
    base, count = _DIO_BANKS[bank]
    if idx >= count:
        raise ValueError(f"{hw!r}: {bank} has only {count} lines")
    return base + idx


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# §4.1 channel map
# ---------------------------------------------------------------------------


class DeviceConfig(StrictModel):
    ip: str
    # How LJM reaches the T7. "ethernet" (production: cabinet LAN via `ip`),
    # "usb" (bench/laptop testing, ignores `ip`), or "any" (first T7 found).
    connection: Literal["ethernet", "usb", "any"] = "ethernet"
    watchdog_timeout_s: int = Field(ge=1, le=60)

    def ljm_open_args(self) -> tuple[str, str]:
        """(connection_type, identifier) for LJM openS."""
        if self.connection == "ethernet":
            return "ETHERNET", self.ip
        return self.connection.upper(), "ANY"

    @field_validator("ip")
    @classmethod
    def _ip_format(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError(f"invalid IPv4 address {v!r}")
        return v


class RateGroup(StrictModel):
    rate_hz: float = Field(gt=0, le=10000)


class PolynomialCal(StrictModel):
    kind: Literal["polynomial"]
    coeffs: list[float] = Field(min_length=1, max_length=8)  # c0 + c1*x + c2*x^2 ...


class LinearCal(StrictModel):
    kind: Literal["linear"]
    gain: float
    offset: float = 0.0


class TableCal(StrictModel):
    kind: Literal["table"]
    x: list[float] = Field(min_length=2)
    y: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _monotonic_x(self) -> TableCal:
        if len(self.x) != len(self.y):
            raise ValueError("cal table: x and y must be the same length")
        if any(b <= a for a, b in zip(self.x, self.x[1:], strict=False)):
            raise ValueError("cal table: x must be strictly increasing")
        return self


Cal = Annotated[PolynomialCal | LinearCal | TableCal, Field(discriminator="kind")]


class AnalogInChannel(StrictModel):
    tag: str
    hw: str
    kind: Literal["analog_in"]
    unit: str
    range: float = Field(default=10.0, description="LJM AIN range setting in volts")
    cal: Cal = PolynomialCal(kind="polynomial", coeffs=[0.0, 1.0])
    rate_group: str = "default"

    @field_validator("hw")
    @classmethod
    def _ain(cls, v: str) -> str:
        m = _AIN_RE.match(v)
        if not m or int(m.group(1)) > 127:  # AIN0-13 native, up to AIN127 with Mux80
            raise ValueError(f"analog_in hw must be AIN0..AIN127, got {v!r}")
        return v


class ThermocoupleChannel(StrictModel):
    tag: str
    hw: str
    kind: Literal["thermocouple_k"]
    unit: Literal["degC", "K", "degF"] = "degC"
    rate_group: str = "default"

    @field_validator("hw")
    @classmethod
    def _ain(cls, v: str) -> str:
        if not _AIN_RE.match(v):
            raise ValueError(f"thermocouple hw must be an AIN, got {v!r}")
        return v


class ValveChannel(StrictModel):
    tag: str
    hw: str
    kind: Literal["valve"]
    fail_safe: Literal["closed", "open"]
    open_state: Literal["high", "low"] = "high"  # electrical polarity

    @field_validator("hw")
    @classmethod
    def _dio(cls, v: str) -> str:
        dio_number(v)
        return v


class SparkChannel(StrictModel):
    tag: str
    hw: str
    kind: Literal["spark"]
    fail_safe: Literal["off"]
    on_state: Literal["high", "low"] = "high"
    max_on_ms: int = Field(default=2000, ge=1, le=60000)  # D9 guard

    @field_validator("hw")
    @classmethod
    def _dio(cls, v: str) -> str:
        dio_number(v)
        return v


Channel = Annotated[
    AnalogInChannel | ThermocoupleChannel | ValveChannel | SparkChannel,
    Field(discriminator="kind"),
]

OutputChannel = ValveChannel | SparkChannel
InputChannel = AnalogInChannel | ThermocoupleChannel

_TAG_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


class ChannelMap(StrictModel):
    device: DeviceConfig
    rate_groups: dict[str, RateGroup]
    channels: list[Channel] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> ChannelMap:
        tags = [c.tag for c in self.channels]
        if dupes := {t for t in tags if tags.count(t) > 1}:
            raise ValueError(f"duplicate channel tags: {sorted(dupes)}")
        for t in tags:
            if not _TAG_RE.match(t):
                raise ValueError(f"tag {t!r} must be dotted lowercase (e.g. pressure.chamber)")
        hws = [c.hw for c in self.channels]
        if dupes := {h for h in hws if hws.count(h) > 1}:
            raise ValueError(f"duplicate hw assignments: {sorted(dupes)}")
        if "default" not in self.rate_groups:
            raise ValueError("rate_groups must define 'default'")
        for c in self.channels:
            if isinstance(c, InputChannel) and c.rate_group not in self.rate_groups:
                raise ValueError(f"{c.tag}: unknown rate_group {c.rate_group!r}")
        outputs = self.outputs()
        if len(outputs) > MAX_OUTPUT_BITS:
            raise ValueError(f"more than {MAX_OUTPUT_BITS} output channels")
        for c in outputs:
            if dio_number(c.hw) >= MAX_OUTPUT_BITS:
                raise ValueError(
                    f"{c.tag}: sequencer outputs must be on FIO0-7/EIO0-7 "
                    f"(DIO_STATE bits 0..15), got {c.hw}"
                )
        return self

    # -- derived views -----------------------------------------------------

    def outputs(self) -> list[OutputChannel]:
        """Output channels sorted by DIO number (deterministic bit order)."""
        outs = [c for c in self.channels if isinstance(c, OutputChannel)]
        return sorted(outs, key=lambda c: dio_number(c.hw))

    def inputs(self) -> list[InputChannel]:
        return [c for c in self.channels if isinstance(c, InputChannel)]

    def bit_index(self, tag: str) -> int:
        """Bit position of an output tag in the 16-bit state word."""
        for c in self.outputs():
            if c.tag == tag:
                return dio_number(c.hw)
        raise KeyError(f"{tag!r} is not an output channel")

    def by_tag(self, tag: str) -> Channel:
        for c in self.channels:
            if c.tag == tag:
                return c
        raise KeyError(f"unknown channel tag {tag!r}")

    def active_level(self, c: OutputChannel) -> int:
        """Electrical level (0/1) meaning 'open' (valve) / 'on' (spark)."""
        pol = c.open_state if isinstance(c, ValveChannel) else c.on_state
        return 1 if pol == "high" else 0

    def safe_level(self, c: OutputChannel) -> int:
        """Electrical level of the fail-safe state."""
        # spark fail_safe is always 'off'; only a valve can fail to its active state
        active = isinstance(c, ValveChannel) and c.fail_safe == "open"
        return self.active_level(c) if active else 1 - self.active_level(c)

    def safe_word(self) -> int:
        word = 0
        for c in self.outputs():
            word |= self.safe_level(c) << dio_number(c.hw)
        return word

    def controlled_mask(self) -> int:
        mask = 0
        for c in self.outputs():
            mask |= 1 << dio_number(c.hw)
        return mask

    def dio_inhibit(self) -> int:
        """§5 header field: bits NOT controlled by the sequencer (16-bit)."""
        return (~self.controlled_mask()) & 0xFFFF

    def spark_channels(self) -> list[SparkChannel]:
        return [c for c in self.channels if isinstance(c, SparkChannel)]


# ---------------------------------------------------------------------------
# §4.2 sequence
# ---------------------------------------------------------------------------

MAX_STEPS = 128
MIN_STEP_SPACING_S = 0.002
_EPS = 1e-9

StepValue = Literal["open", "close", "on", "off"]


class Precondition(StrictModel):
    tag: str
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _bounds(self) -> Precondition:
        if self.min is None and self.max is None:
            raise ValueError("precondition needs min and/or max")
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("precondition min must be < max")
        return self


class SeqStep(StrictModel):
    t: float = Field(ge=0)
    set: dict[str, StepValue] = Field(min_length=1)


class Redline(StrictModel):
    """Host-side auto-abort limit, evaluated from the live ring buffer while
    the sequence is ARMED or RUNNING. A violation must persist for
    `persist_ms` before tripping (debounces sensor noise). Redlines are a
    monitoring layer on top of the Lua safety chain — the abort they trigger
    takes the same always-accepted fastest path as the operator button."""

    tag: str
    min: float | None = None
    max: float | None = None
    persist_ms: int = Field(default=50, ge=0, le=5000)

    @model_validator(mode="after")
    def _bounds(self) -> Redline:
        if self.min is None and self.max is None:
            raise ValueError("redline needs min and/or max")
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("redline min must be < max")
        return self


def _check_steps(steps: list[SeqStep], what: str, max_duration_s: float | None) -> None:
    if len(steps) > MAX_STEPS:
        raise ValueError(f"{what}: {len(steps)} steps exceeds max {MAX_STEPS}")
    for prev, cur in itertools.pairwise(steps):
        if cur.t < prev.t - _EPS:
            raise ValueError(f"{what}: step times must be non-decreasing (t={cur.t})")
        if cur.t - prev.t < MIN_STEP_SPACING_S - _EPS:
            raise ValueError(
                f"{what}: steps at t={prev.t} and t={cur.t} closer than "
                f"{MIN_STEP_SPACING_S * 1000:.0f} ms"
            )
    if max_duration_s is not None and steps and steps[-1].t > max_duration_s + _EPS:
        raise ValueError(
            f"{what}: last step at t={steps[-1].t} exceeds max_duration_s={max_duration_s}"
        )


class Sequence(StrictModel):
    name: str
    description: str = ""
    max_duration_s: float = Field(gt=0, le=600)
    preconditions: list[Precondition] = []
    steps: list[SeqStep] = Field(min_length=1)
    abort: list[SeqStep] = Field(min_length=1)
    allow_unsafe_end: bool = False
    redlines: list[Redline] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(f"sequence name {v!r} must be [a-zA-Z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _validate(self) -> Sequence:
        _check_steps(self.steps, "steps", self.max_duration_s)
        _check_steps(self.abort, "abort", None)
        return self


def cross_validate_sequence(seq: Sequence, chmap: ChannelMap) -> list[str]:
    """Channel-map-dependent checks (§4.2). Returns a list of error strings.

    Word-expansion rules (carry-forward, unsafe-end, static spark on-time)
    are enforced by the step-table compiler, which reuses these checks first.
    """
    errors: list[str] = []
    output_tags = {c.tag for c in chmap.outputs()}
    input_tags = {c.tag for c in chmap.inputs()}
    for what, steps in (("steps", seq.steps), ("abort", seq.abort)):
        for i, step in enumerate(steps):
            for tag, value in step.set.items():
                if tag not in output_tags:
                    errors.append(f"{what}[{i}]: {tag!r} is not an output channel")
                    continue
                ch = chmap.by_tag(tag)
                if isinstance(ch, ValveChannel) and value not in ("open", "close"):
                    errors.append(f"{what}[{i}]: valve {tag!r} takes open/close, got {value!r}")
                if isinstance(ch, SparkChannel) and value not in ("on", "off"):
                    errors.append(f"{what}[{i}]: spark {tag!r} takes on/off, got {value!r}")
    for i, pre in enumerate(seq.preconditions):
        if pre.tag not in input_tags:
            errors.append(f"preconditions[{i}]: {pre.tag!r} is not an input channel")
    for i, rl in enumerate(seq.redlines):
        if rl.tag not in input_tags:
            errors.append(f"redlines[{i}]: {rl.tag!r} is not an input channel")
    return errors


# ---------------------------------------------------------------------------
# §4.3 dashboard
# ---------------------------------------------------------------------------

PanelType = Literal["timeseries", "value_table", "valve_panel", "sequence_panel", "recorder_panel"]


class GridConfig(StrictModel):
    cols: int = Field(default=12, ge=1, le=48)
    row_height_px: int = Field(default=40, ge=8, le=400)


class YAxisConfig(StrictModel):
    mode: Literal["auto", "manual"] = "auto"
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _manual_bounds(self) -> YAxisConfig:
        if self.mode == "manual" and (self.min is None or self.max is None):
            raise ValueError("manual y_axis needs min and max")
        return self


class Panel(StrictModel):
    id: str
    type: PanelType
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=1)
    h: int = Field(ge=1)
    channels: list[str] = []  # tags or glob patterns (value_table)
    y_axis: YAxisConfig = YAxisConfig()


class Dashboard(StrictModel):
    name: str
    grid: GridConfig = GridConfig()
    panels: list[Panel] = []

    @model_validator(mode="after")
    def _validate(self) -> Dashboard:
        ids = [p.id for p in self.panels]
        if dupes := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"duplicate panel ids: {sorted(dupes)}")
        for p in self.panels:
            if p.x + p.w > self.grid.cols:
                raise ValueError(
                    f"panel {p.id}: x+w={p.x + p.w} exceeds grid cols={self.grid.cols}"
                )
        return self
