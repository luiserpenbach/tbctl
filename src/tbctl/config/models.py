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

# T7 analog input settings, written to the device before every stream start.
# AIN#_RANGE takes one of these (+-V). AIN#_NEGATIVE_CH is 199 for
# single-ended (vs GND) or the negative AIN of a differential pair: on the
# base inputs the pair is an even AIN and the next odd one (AIN0/AIN1 ...
# AIN12/AIN13); on Mux80 extended inputs (AIN16-127) it is n and n+8 with n
# in the lower half of its 16-channel block (AIN48/AIN56 ... AIN55/AIN63).
AIN_RANGES = (10.0, 1.0, 0.1, 0.01)
SINGLE_ENDED = 199
# A channel with `average: true` records the mean of all stream scans in each
# of its sample periods; the stream then runs at least this fast. At 50 Hz
# that is 20 scans = one 50 Hz mains period, so hum and its harmonics cancel.
AVERAGE_STREAM_HZ = 1000.0


def ain_number(hw: str) -> int:
    m = _AIN_RE.match(hw)
    if not m:
        raise ValueError(f"{hw!r} is not an AIN name")
    return int(m.group(1))


def differential_error(pos_hw: str, neg_hw: str) -> str | None:
    """Why (pos, neg) is not a valid T7 differential pair, or None."""
    p, n = ain_number(pos_hw), ain_number(neg_hw)
    if p <= 13:
        if p % 2 == 0 and n == p + 1:
            return None
        return f"{pos_hw}: base differential pairs are even/odd neighbours (AIN{p - p % 2}/AIN{p - p % 2 + 1})"
    if p >= 16:
        if (p // 8) % 2 == 0 and n == p + 8:
            return None
        return f"{pos_hw}: extended differential pairs are AINn/AIN(n+8), n in the lower half of its 16-block"
    return f"{pos_hw}: AIN14/AIN15 cannot be differential"


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
    # Stream FIO_EIO_STATE alongside the analog inputs and record every
    # output (valve / igniter) as a 0/1 channel (1 = open / on) in the
    # fastest rate group. hopper-test-lab times ignition from these edges.
    record_outputs: bool = True

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


def _check_range(v: float) -> float:
    if v not in AIN_RANGES:
        raise ValueError(f"range must be one of {AIN_RANGES} (T7 +-V), got {v}")
    return v


def _check_differential(ch):
    if ch.negative_ch is not None:
        if not _AIN_RE.match(ch.negative_ch):
            raise ValueError(f"{ch.tag}: negative_ch must be an AIN name, got {ch.negative_ch!r}")
        if err := differential_error(ch.hw, ch.negative_ch):
            raise ValueError(f"{ch.tag}: {err}")
    return ch


class AnalogInChannel(StrictModel):
    tag: str
    # display / P&ID name (e.g. PT-CC-01); becomes the channel name in
    # hopper-test-lab exports. Empty = use the tag.
    name: str = Field(default="", max_length=40)
    hw: str
    kind: Literal["analog_in"]
    unit: str
    range: float = Field(default=10.0, description="T7 AIN range, +-V (10, 1, 0.1, 0.01)")
    # negative input of a differential pair (e.g. AIN7 for AIN6); None = single-ended
    negative_ch: str | None = None
    cal: Cal = PolynomialCal(kind="polynomial", coeffs=[0.0, 1.0])
    rate_group: str = "default"
    # mean of the stream scans in each sample period (see AVERAGE_STREAM_HZ)
    average: bool = False

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
        return _check_range(v)

    @model_validator(mode="after")
    def _differential(self) -> AnalogInChannel:
        return _check_differential(self)


class ThermocoupleChannel(StrictModel):
    tag: str
    # display / P&ID name (e.g. PT-CC-01); becomes the channel name in
    # hopper-test-lab exports. Empty = use the tag.
    name: str = Field(default="", max_length=40)
    hw: str
    kind: Literal["thermocouple_k"]
    unit: Literal["degC", "K", "degF"] = "degC"
    # type K is ~41 uV/degC (~55 mV at 1372 degC): +-0.1 V keeps the resolution
    range: float = 0.1
    negative_ch: str | None = None  # thermocouples are usually wired differential
    rate_group: str = "default"
    # mean of the stream scans in each sample period (see AVERAGE_STREAM_HZ)
    average: bool = False

    @field_validator("hw")
    @classmethod
    def _ain(cls, v: str) -> str:
        if not _AIN_RE.match(v):
            raise ValueError(f"thermocouple hw must be an AIN, got {v!r}")
        return v

    @field_validator("range")
    @classmethod
    def _range(cls, v: float) -> float:
        return _check_range(v)

    @model_validator(mode="after")
    def _differential(self) -> ThermocoupleChannel:
        return _check_differential(self)


class ValveChannel(StrictModel):
    tag: str
    # display / P&ID name (e.g. PT-CC-01); becomes the channel name in
    # hopper-test-lab exports. Empty = use the tag.
    name: str = Field(default="", max_length=40)
    hw: str
    kind: Literal["valve"]
    fail_safe: Literal["closed", "open"]
    open_state: Literal["high", "low"] = "high"  # electrical polarity

    @field_validator("hw")
    @classmethod
    def _dio(cls, v: str) -> str:
        dio_number(v)
        return v


class IgniterChannel(StrictModel):
    tag: str
    # display / P&ID name (e.g. PT-CC-01); becomes the channel name in
    # hopper-test-lab exports. Empty = use the tag.
    name: str = Field(default="", max_length=40)
    hw: str
    kind: Literal["igniter"]
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
    # display / P&ID name (e.g. PT-CC-01); becomes the channel name in
    # hopper-test-lab exports. Empty = use the tag.
    name: str = Field(default="", max_length=40)
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
    AnalogInChannel | ThermocoupleChannel | ValveChannel | IgniterChannel | DerivedChannel,
    Field(discriminator="kind"),
]

OutputChannel = ValveChannel | IgniterChannel
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
        names = [c.name for c in self.channels if c.name]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate channel names: {sorted(dupes)}")
        hws = [c.hw for c in self.channels if not isinstance(c, DerivedChannel)]
        if dupes := {h for h in hws if hws.count(h) > 1}:
            raise ValueError(f"duplicate hw assignments: {sorted(dupes)}")
        for c in self.inputs():
            if c.negative_ch is not None and c.negative_ch in hws:
                raise ValueError(
                    f"{c.tag}: {c.negative_ch} is its differential negative input, "
                    "so no channel can use it"
                )
        if "default" not in self.rate_groups:
            raise ValueError("rate_groups must define 'default'")
        for c in self.channels:
            if isinstance(c, InputChannel) and c.rate_group not in self.rate_groups:
                raise ValueError(f"{c.tag}: unknown rate_group {c.rate_group!r}")
        if any(c.average for c in self.inputs()):
            stream = self.stream_rate_hz()
            for c in self.inputs():
                rate = self.rate_groups[c.rate_group].rate_hz
                if abs(stream / rate - round(stream / rate)) > 1e-9:
                    raise ValueError(
                        f"{c.tag}: rate group {c.rate_group} ({rate:g} Hz) must divide the "
                        f"stream rate, {stream:g} Hz with averaging on"
                    )
                if c.average and rate >= stream:
                    raise ValueError(
                        f"{c.tag}: average has no effect at {rate:g} Hz, the stream rate "
                        "(each sample is a single scan)"
                    )
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
        return self

    # -- derived views -----------------------------------------------------

    def outputs(self) -> list[OutputChannel]:
        """Output channels sorted by DIO number (deterministic bit order)."""
        outs = [c for c in self.channels if isinstance(c, OutputChannel)]
        return sorted(outs, key=lambda c: dio_number(c.hw))

    def inputs(self) -> list[InputChannel]:
        """Physical (hardware-scanned) input channels."""
        return [c for c in self.channels if isinstance(c, InputChannel)]

    def stream_rate_hz(self) -> float:
        """T7 scan rate: the fastest input rate group, raised to
        AVERAGE_STREAM_HZ when any channel averages."""
        inputs = self.inputs()
        fastest = max((self.rate_groups[c.rate_group].rate_hz for c in inputs), default=50.0)
        if any(c.average for c in inputs):
            return max(fastest, AVERAGE_STREAM_HZ)
        return fastest

    def ain_settings(self) -> list[tuple[str, float]]:
        """(register, value) pairs configuring every scanned AIN: range and
        negative channel. Written before each stream start."""
        out: list[tuple[str, float]] = []
        for c in self.inputs():
            neg = ain_number(c.negative_ch) if c.negative_ch else SINGLE_ENDED
            out += [(f"{c.hw}_RANGE", c.range), (f"{c.hw}_NEGATIVE_CH", float(neg))]
        return out

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
        """Electrical level (0/1) meaning 'open' (valve) / 'on' (igniter)."""
        pol = c.open_state if isinstance(c, ValveChannel) else c.on_state
        return 1 if pol == "high" else 0

    def safe_level(self, c: OutputChannel) -> int:
        """Electrical level of the fail-safe state."""
        # igniter fail_safe is always 'off'; only a valve can fail to its active state
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

    def igniter_channels(self) -> list[IgniterChannel]:
        return [c for c in self.channels if isinstance(c, IgniterChannel)]


# ---------------------------------------------------------------------------
# §4.2 sequence
# ---------------------------------------------------------------------------

MAX_STEPS = 128
MIN_STEP_SPACING_S = 0.002
_EPS = 1e-9

StepValue = Literal["open", "close", "on", "off"]

# hopper-test-lab test types (TestType there); stamped on runs that fire the
# sequence so the analysis side knows which kind of test it is.
TestType = Literal["engine", "igniter", "cold_flow"]


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
    test_type: TestType | None = None  # None = not a test (e.g. timing check)
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

    Word-expansion rules (carry-forward, unsafe-end, static igniter on-time)
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
                if isinstance(ch, IgniterChannel) and value not in ("on", "off"):
                    errors.append(f"{what}[{i}]: igniter {tag!r} takes on/off, got {value!r}")
    for i, pre in enumerate(seq.preconditions):
        if pre.tag not in input_tags:
            errors.append(f"preconditions[{i}]: {pre.tag!r} is not an input channel")
    for i, rl in enumerate(seq.redlines):
        if rl.tag not in input_tags:
            errors.append(f"redlines[{i}]: {rl.tag!r} is not an input channel")
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


def cross_validate_review(cfg: ReviewConfig, chmap: ChannelMap) -> list[str]:
    readable = chmap.readable_tags()
    return [
        f"rule {r.name}: {r.tag!r} is not a readable channel"
        for r in cfg.rules
        if r.tag not in readable
    ]


# ---------------------------------------------------------------------------
# P&ID mimic (roadmap v2 phase 13): background SVG + channel-bound elements.
# Positions are normalized 0-1 against the background image box so the
# panel scales with panel size.
# ---------------------------------------------------------------------------

_ASSET_RE = re.compile(r"^[a-zA-Z0-9_\-]+\.svg$")


class PidValveElement(StrictModel):
    id: str
    type: Literal["valve"]
    channel: str  # must be an output channel (valve or igniter)
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
# Simulator plant model (configs/sim.yaml, --sim only; hardware never reads
# it). A first-order chamber: lights when both propellant valves are open and
# the igniter has been on, then Pc lags to pc_bar; thrust and manifold
# pressures follow Pc. Contributions add to the /api/sim/set setpoints.
# ---------------------------------------------------------------------------


class SimThrust(StrictModel):
    tag: str
    n_per_bar: float = Field(gt=0)


class SimManifold(StrictModel):
    tag: str
    valve: str
    dp_frac: float = Field(default=0.2, ge=0, le=5)  # injector dp as a fraction of Pc


class SimChamber(StrictModel):
    tag: str  # chamber pressure input
    ox_valve: str
    fuel_valve: str
    igniter: str | None = None  # None = lights without one (e.g. hypergolic)
    pc_bar: float = Field(gt=0)  # steady state above the setpoint, while lit
    cold_flow_bar: float = Field(default=1.0, ge=0)  # propellant flowing, not lit
    ignition_delay_ms: int = Field(default=80, ge=0, le=5000)
    rise_ms: float = Field(default=40.0, gt=0, le=10000)  # time constants
    decay_ms: float = Field(default=60.0, gt=0, le=10000)
    roughness: float = Field(default=0.02, ge=0, le=0.5)  # fractional noise while lit
    thrust: SimThrust | None = None
    manifolds: list[SimManifold] = []


class SimConfig(StrictModel):
    chamber: SimChamber | None = None


def cross_validate_sim(cfg: SimConfig, chmap: ChannelMap) -> list[str]:
    c = cfg.chamber
    if c is None:
        return []
    inputs = {ch.tag for ch in chmap.inputs()}
    valves = {ch.tag for ch in chmap.outputs() if isinstance(ch, ValveChannel)}
    igniters = {ch.tag for ch in chmap.igniter_channels()}
    errors = []
    want = [("tag", c.tag, inputs, "input"), ("ox_valve", c.ox_valve, valves, "valve")]
    want += [("fuel_valve", c.fuel_valve, valves, "valve")]
    if c.igniter is not None:
        want.append(("igniter", c.igniter, igniters, "igniter"))
    if c.thrust is not None:
        want.append(("thrust.tag", c.thrust.tag, inputs, "input"))
    for i, m in enumerate(c.manifolds):
        want += [(f"manifolds[{i}].tag", m.tag, inputs, "input")]
        want += [(f"manifolds[{i}].valve", m.valve, valves, "valve")]
    for field_name, tag, allowed, what in want:
        if tag not in allowed:
            errors.append(f"chamber.{field_name}: {tag!r} is not an {what} channel")
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
