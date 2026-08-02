"""Pydantic v2 models for all on-disk YAML configuration (spec §4).

Validation failure anywhere = refuse to start / refuse to load. Cross-file
checks (sequence tags vs. channel map) live in `cross_validate_sequence`;
full step-table compilation is the sequencer compiler's job (§4.2 rules that
need word expansion are enforced there).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Collection
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Wire-format limits come from the generated register constants so
# registers.yaml stays the single source of truth (never hardcode them here).
from tbctl.hw.registers_gen import MAX_STEPS, MIN_STEP_SPACING_MS

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


# T7 AIN gains the LJM driver actually supports; anything else is silently
# coerced by LJM, so refuse it at validation time.
LJM_AIN_RANGES = (10.0, 1.0, 0.1, 0.01)


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

    @field_validator("range")
    @classmethod
    def _range(cls, v: float) -> float:
        if v not in LJM_AIN_RANGES:
            raise ValueError(
                f"range must be one of {sorted(LJM_AIN_RANGES, reverse=True)} V "
                f"(LJM-supported AIN gains), got {v!r}"
            )
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
        m = _AIN_RE.match(v)
        if not m or int(m.group(1)) > 127:  # same AIN space as analog_in (Mux80 max)
            raise ValueError(f"thermocouple hw must be AIN0..AIN127, got {v!r}")
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


class DerivedChannel(StrictModel):
    """Calculated channel: an expression over physical input channels,
    computed in the acquisition path so it flows through live charts, the
    recorder, redlines and CSV export like any measured channel. Runs in
    the rate group of its fastest referenced channel (slower references
    are sample-and-hold)."""

    tag: str
    kind: Literal["derived"]
    unit: str = ""
    expr: str

    @field_validator("expr")
    @classmethod
    def _expr(cls, v: str) -> str:
        from tbctl.config.expr import ExprError, expr_refs

        try:
            expr_refs(v)
        except ExprError as e:
            raise ValueError(f"bad expression: {e}") from e
        return v


Channel = Annotated[
    AnalogInChannel | ThermocoupleChannel | ValveChannel | SparkChannel | DerivedChannel,
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
        hws = [c.hw for c in self.channels if not isinstance(c, DerivedChannel)]
        if dupes := {h for h in hws if hws.count(h) > 1}:
            raise ValueError(f"duplicate hw assignments: {sorted(dupes)}")
        if "default" not in self.rate_groups:
            raise ValueError("rate_groups must define 'default'")
        for c in self.channels:
            if isinstance(c, InputChannel) and c.rate_group not in self.rate_groups:
                raise ValueError(f"{c.tag}: unknown rate_group {c.rate_group!r}")
        # derived channels: refs must be existing physical inputs (no chaining)
        physical = {c.tag for c in self.channels if isinstance(c, InputChannel)}
        from tbctl.config.expr import expr_refs

        for c in self.channels:
            if isinstance(c, DerivedChannel):
                for ref in expr_refs(c.expr):
                    if ref not in physical:
                        raise ValueError(
                            f"{c.tag}: expression references {ref!r}, which is not a "
                            "physical input channel (derived channels cannot chain)"
                        )
        outputs = self.outputs()
        if len(outputs) > MAX_OUTPUT_BITS:
            raise ValueError(f"more than {MAX_OUTPUT_BITS} output channels")
        for c in outputs:
            if dio_number(c.hw) >= MAX_OUTPUT_BITS:
                raise ValueError(
                    f"{c.tag}: sequencer outputs must be on FIO0-7/EIO0-7 "
                    f"(DIO_STATE bits 0..15), got {c.hw}"
                )
        # The stream needs at least one scanned channel: an output-only map
        # passes sim but fails eStreamStart on hardware, and leaves redlines
        # and preconditions with nothing to evaluate.
        inputs = [c for c in self.channels if isinstance(c, InputChannel)]
        if not inputs:
            raise ValueError(
                "channel map needs at least one input channel "
                "(analog_in / thermocouple_k): the acquisition stream cannot start without one"
            )
        # Rate-group divisibility — mirrors AcquisitionService._configure
        # exactly: the stream runs at the fastest rate among the groups that
        # input channels actually use, and every used group must decimate
        # from it by an integer factor. Validating here means a channel map
        # that would crash acquisition at (re)configure time can never load.
        used_groups = {c.rate_group for c in inputs}
        scan_rate_hz = max(self.rate_groups[g].rate_hz for g in used_groups)
        for g in sorted(used_groups):
            rate_hz = self.rate_groups[g].rate_hz
            factor = scan_rate_hz / rate_hz
            if abs(factor - round(factor)) > 1e-9:
                raise ValueError(
                    f"rate group {g}: {rate_hz} Hz must divide stream rate {scan_rate_hz} Hz"
                )
        return self

    # -- derived views -----------------------------------------------------

    def outputs(self) -> list[OutputChannel]:
        """Output channels sorted by DIO number (deterministic bit order)."""
        outs = [c for c in self.channels if isinstance(c, OutputChannel)]
        return sorted(outs, key=lambda c: dio_number(c.hw))

    def inputs(self) -> list[InputChannel]:
        """Physical (hardware-scanned) input channels."""
        return [c for c in self.channels if isinstance(c, InputChannel)]

    def derived(self) -> list[DerivedChannel]:
        return [c for c in self.channels if isinstance(c, DerivedChannel)]

    def derived_group(self, dc: DerivedChannel) -> str:
        """Rate group a derived channel is computed on: the fastest group
        among its referenced channels (ties: first reference wins)."""
        from tbctl.config.expr import expr_refs

        groups = [self.by_tag(ref).rate_group for ref in expr_refs(dc.expr)]
        return max(groups, key=lambda g: self.rate_groups[g].rate_hz)

    def readable_tags(self) -> set[str]:
        """Tags that produce values: physical inputs + derived."""
        return {c.tag for c in self.inputs()} | {c.tag for c in self.derived()}

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

MIN_STEP_SPACING_S = MIN_STEP_SPACING_MS / 1000.0
# The on-device Lua rejects any step time above one hour (BAD_TABLE); mirror
# that bound host-side for every table (main AND abort) so it can never
# surface as a device fault or an unpackable table.
MAX_STEP_T_MS = 3_600_000
MAX_STEP_T_S = MAX_STEP_T_MS / 1000.0
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
    for step in steps:
        if step.t > MAX_STEP_T_S + _EPS:
            raise ValueError(
                f"{what}: step at t={step.t} exceeds the device's "
                f"{MAX_STEP_T_S:.0f} s (1 h) step-time limit"
            )
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
        # The abort table must act immediately: its first step is the word
        # applied the instant ABORT is taken, so any positive start time is
        # pure added latency with the engine still in its firing state.
        if self.abort and self.abort[0].t > _EPS:
            raise ValueError(
                f"abort: first step must be at t=0 (got t={self.abort[0].t}); "
                "aborts must apply their first word with zero latency"
            )
        return self


def _table_cal_limit_errors(
    what: str, i: int, tag: str, min_: float | None, max_: float | None, chmap: ChannelMap
) -> list[str]:
    """A TableCal channel clamps (np.interp) to its table's y-range, so a
    limit at/outside that range can never trip (dead redline / vacuous
    precondition) or can never pass — either way the config is wrong."""
    try:
        ch = chmap.by_tag(tag)
    except KeyError:
        return []
    if not (isinstance(ch, AnalogInChannel) and isinstance(ch.cal, TableCal)):
        return []
    y_lo, y_hi = min(ch.cal.y), max(ch.cal.y)
    errors: list[str] = []
    # `min` bound: reachable values are [y_lo, y_hi]; v < min can only ever
    # be observed when y_lo < min, and v >= min can only hold when min <= y_hi.
    if min_ is not None and not (y_lo < min_ <= y_hi):
        errors.append(
            f"{what}[{i}]: min {min_} is outside the calibration table's "
            f"representable range [{y_lo}, {y_hi}] of {tag!r} — the clamped "
            "cal makes this limit dead or impossible"
        )
    if max_ is not None and not (y_lo <= max_ < y_hi):
        errors.append(
            f"{what}[{i}]: max {max_} is outside the calibration table's "
            f"representable range [{y_lo}, {y_hi}] of {tag!r} — the clamped "
            "cal makes this limit dead or impossible"
        )
    return errors


def cross_validate_sequence(seq: Sequence, chmap: ChannelMap) -> list[str]:
    """Channel-map-dependent checks (§4.2). Returns a list of error strings.

    Word-expansion rules (carry-forward, unsafe-end, static spark on-time)
    are enforced by the step-table compiler, which reuses these checks first.
    """
    errors: list[str] = []
    output_tags = {c.tag for c in chmap.outputs()}
    input_tags = chmap.readable_tags()  # physical + derived
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
        else:
            errors += _table_cal_limit_errors("preconditions", i, pre.tag, pre.min, pre.max, chmap)
    for i, rl in enumerate(seq.redlines):
        if rl.tag not in input_tags:
            errors.append(f"redlines[{i}]: {rl.tag!r} is not an input channel")
        else:
            errors += _table_cal_limit_errors("redlines", i, rl.tag, rl.min, rl.max, chmap)
    return errors


# ---------------------------------------------------------------------------
# Post-run review rules (Sift-style automated data review). Evaluated over
# recorded data when a run stops — a monitoring/QA layer, never an abort
# path (that's what redlines are for).
# ---------------------------------------------------------------------------

ReviewMetric = Literal["max", "min", "mean", "rms"]


class ReviewRule(StrictModel):
    name: str
    tag: str  # any readable channel (physical or derived)
    metric: ReviewMetric
    # window "run" = whole recording; "fire" = seq_fired .. sequence end/abort
    window: Literal["run", "fire"] = "run"
    limit_min: float | None = None
    limit_max: float | None = None
    # only evaluate for runs that fired one of these sequences ([] = all runs)
    sequences: list[str] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(f"rule name {v!r} must be [a-zA-Z0-9_-]+")
        return v

    @model_validator(mode="after")
    def _bounds(self) -> ReviewRule:
        if self.limit_min is None and self.limit_max is None:
            raise ValueError("review rule needs limit_min and/or limit_max")
        if (
            self.limit_min is not None
            and self.limit_max is not None
            and self.limit_min >= self.limit_max
        ):
            raise ValueError("limit_min must be < limit_max")
        return self


class ReviewConfig(StrictModel):
    rules: list[ReviewRule] = []

    @model_validator(mode="after")
    def _validate(self) -> ReviewConfig:
        names = [r.name for r in self.rules]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate rule names: {sorted(dupes)}")
        return self


def cross_validate_review(
    cfg: ReviewConfig,
    chmap: ChannelMap,
    sequence_names: Collection[str] | None = None,
) -> list[str]:
    readable = chmap.readable_tags()
    errors = [
        f"rule {r.name}: {r.tag!r} is not a readable channel"
        for r in cfg.rules
        if r.tag not in readable
    ]
    # `sequences:` scoping must reference sequences that actually exist —
    # otherwise a renamed sequence silently disables the rule (reports just
    # shrink). Only checked when the caller knows the sequence set.
    if sequence_names is not None:
        known = set(sequence_names)
        for r in cfg.rules:
            for s in r.sequences:
                if s not in known:
                    errors.append(f"rule {r.name}: sequences: references unknown sequence {s!r}")
    return errors


# ---------------------------------------------------------------------------
# P&ID mimic (roadmap v2 phase 13): background SVG + channel-bound elements.
# Positions are normalized 0-1 against the background image box so the
# panel scales with panel size.
# ---------------------------------------------------------------------------

_ASSET_RE = re.compile(r"^[a-zA-Z0-9_\-]+\.svg$")


class PidValveElement(StrictModel):
    id: str
    type: Literal["valve"]
    channel: str  # must be an output channel (valve or spark)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    size: float = Field(default=0.05, gt=0, le=0.5)  # fraction of canvas width
    rotation: float = Field(default=0.0, ge=-360, le=360)


class PidSensorElement(StrictModel):
    id: str
    type: Literal["sensor"]
    channel: str  # must be an input channel
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    decimals: int = Field(default=1, ge=0, le=5)
    warn_min: float | None = None
    warn_max: float | None = None

    @model_validator(mode="after")
    def _warn_bounds(self) -> PidSensorElement:
        if (
            self.warn_min is not None
            and self.warn_max is not None
            and self.warn_min >= self.warn_max
        ):
            raise ValueError("warn_min must be < warn_max")
        return self


class PidLabelElement(StrictModel):
    id: str
    type: Literal["label"]
    text: str
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    size: float = Field(default=0.022, gt=0, le=0.2)  # font height, fraction of width


PidElement = Annotated[
    PidValveElement | PidSensorElement | PidLabelElement,
    Field(discriminator="type"),
]


class PidConfig(StrictModel):
    name: str
    background: str | None = None  # asset filename under configs/pid/assets/
    elements: list[PidElement] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(f"pid name {v!r} must be [a-zA-Z0-9_-]+")
        return v

    @field_validator("background")
    @classmethod
    def _background(cls, v: str | None) -> str | None:
        # plain filename only — no path separators, must be an .svg asset
        if v is not None and not _ASSET_RE.match(v):
            raise ValueError(f"background {v!r} must be a plain <name>.svg asset filename")
        return v

    @model_validator(mode="after")
    def _validate(self) -> PidConfig:
        ids = [e.id for e in self.elements]
        if dupes := {i for i in ids if ids.count(i) > 1}:
            raise ValueError(f"duplicate element ids: {sorted(dupes)}")
        return self


def cross_validate_pid(pid: PidConfig, chmap: ChannelMap) -> list[str]:
    """Channel-map-dependent checks. Returns a list of error strings."""
    errors: list[str] = []
    output_tags = {c.tag for c in chmap.outputs()}
    input_tags = chmap.readable_tags()  # physical + derived
    for e in pid.elements:
        if isinstance(e, PidValveElement) and e.channel not in output_tags:
            errors.append(f"element {e.id}: {e.channel!r} is not an output channel")
        if isinstance(e, PidSensorElement) and e.channel not in input_tags:
            errors.append(f"element {e.id}: {e.channel!r} is not an input channel")
    return errors


# ---------------------------------------------------------------------------
# §4.3 dashboard
# ---------------------------------------------------------------------------

PanelType = Literal[
    "timeseries",
    "value_table",
    "valve_panel",
    "sequence_panel",
    "recorder_panel",
    "raw_table",
    "sim_panel",
    "pid",
    "checklist_panel",
    "xy",
]


class GridConfig(StrictModel):
    cols: int = Field(default=12, ge=1, le=48)
    row_height_px: int = Field(default=40, ge=8, le=400)


class YAxisConfig(StrictModel):
    mode: Literal["auto", "manual"] = "auto"
    min: float | None = None
    max: float | None = None
    log: bool = False  # log10 y scale (positive data only)
    label: str | None = None  # axis label, e.g. "pressure [bar]"

    @model_validator(mode="after")
    def _manual_bounds(self) -> YAxisConfig:
        if self.mode == "manual" and (self.min is None or self.max is None):
            raise ValueError("manual y_axis needs min and max")
        if (
            self.mode == "manual"
            and self.min is not None
            and self.max is not None
            and self.min >= self.max
        ):
            raise ValueError("manual y_axis needs min < max")
        if self.log and self.mode == "manual" and self.min is not None and self.min <= 0:
            raise ValueError("log y_axis needs min > 0")
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
    pid: str | None = None  # which P&ID config a `pid` panel shows
    title: str | None = None  # custom header text (defaults to the type name)
    window_s: float | None = Field(default=None, gt=0, le=600)  # live time window
    show_legend: bool = True


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


_GLOB_CHARS = ("*", "?", "[")


def cross_validate_dashboard(
    dash: Dashboard,
    chmap: ChannelMap,
    pid_names: Collection[str] | None = None,
) -> list[str]:
    """Channel-map-dependent checks: literal panel channel tags must exist
    (glob patterns are matched at render time and are skipped here), and a
    `pid` panel must reference an existing P&ID config when the caller knows
    the P&ID set. Returns a list of error strings."""
    errors: list[str] = []
    known = chmap.readable_tags() | {c.tag for c in chmap.outputs()}
    for p in dash.panels:
        for tag in p.channels:
            if any(ch in tag for ch in _GLOB_CHARS):
                continue  # glob pattern (value_table): resolved at render time
            if tag not in known:
                errors.append(f"panel {p.id}: channel {tag!r} is not in the channel map")
        if p.type == "pid":
            if p.pid is None:
                errors.append(f"panel {p.id}: pid panel needs a `pid:` config name")
            elif pid_names is not None and p.pid not in set(pid_names):
                errors.append(f"panel {p.id}: unknown pid config {p.pid!r}")
    return errors
