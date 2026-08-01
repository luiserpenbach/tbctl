"""Sequence compiler (spec §4.2): validated Sequence + ChannelMap -> StepTable.

Expands sparse per-tag steps into full-state dio words (D7) with
carry-forward from safe_word, and enforces every §4.2 rule that needs word
expansion:

  - final main-table state must equal safe_word, unless the sequence sets
    `allow_unsafe_end: true` (then it becomes a loud warning)
  - the abort table's final state must equal safe_word — always a hard error
  - static spark on-time within the table must not exceed max_on_ms
    (the Lua guard is the dynamic backstop, D9)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tbctl.config.models import (
    ChannelMap,
    Sequence,
    cross_validate_sequence,
    dio_number,
)
from tbctl.hw.registers_gen import MIN_STEP_SPACING_MS
from tbctl.seqtable import Step, StepTable


class CompileError(Exception):
    """Sequence failed to compile. str() lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class CompiledSequence:
    name: str
    table: StepTable
    duration_ms: int
    abort_duration_ms: int
    warnings: list[str] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "name": self.name,
            "n_steps": len(self.table.steps),
            "n_abort_steps": len(self.table.abort),
            "duration_ms": self.duration_ms,
            "abort_duration_ms": self.abort_duration_ms,
            "safe_word": self.table.safe_word,
            "dio_inhibit": self.table.dio_inhibit,
            "spark_guard_ms": self.table.spark_guard_ms,
            "spark_mask": self.table.spark_mask,
            "crc16": self.table.crc16,
            "size_bytes": len(self.table.pack()),
            "warnings": self.warnings,
        }


def _expand(
    seq_steps, chmap: ChannelMap, initial_word: int, what: str, errors: list[str]
) -> list[Step]:
    """Sparse steps -> full-state words with carry-forward (D7)."""
    word = initial_word
    out: list[Step] = []
    prev_t_ms: int | None = None
    for i, step in enumerate(seq_steps):
        t_ms = round(step.t * 1000.0)
        if prev_t_ms is not None:
            if t_ms < prev_t_ms:
                errors.append(f"{what}[{i}]: t not monotonic at ms resolution")
            elif t_ms - prev_t_ms < MIN_STEP_SPACING_MS:
                errors.append(
                    f"{what}[{i}]: {t_ms - prev_t_ms} ms after previous step "
                    f"(min {MIN_STEP_SPACING_MS} ms)"
                )
        prev_t_ms = t_ms
        for tag, value in step.set.items():
            ch = chmap.by_tag(tag)
            bit = dio_number(ch.hw)
            active = value in ("open", "on")
            level = chmap.active_level(ch) if active else 1 - chmap.active_level(ch)
            word = (word | (1 << bit)) if level else (word & ~(1 << bit))
        out.append(Step(t_ms, word & 0xFFFF))
    return out


def _max_spark_on_ms(steps: list[Step], end_ms: int, spark_mask: int) -> int:
    """Longest continuous spark-high interval implied by the table."""
    longest = 0
    on_since: int | None = None
    for s in steps:
        if s.dio_word & spark_mask:
            if on_since is None:
                on_since = s.t_ms
        elif on_since is not None:
            longest = max(longest, s.t_ms - on_since)
            on_since = None
    if on_since is not None:
        longest = max(longest, end_ms - on_since)
    return longest


def compile_sequence(seq: Sequence, chmap: ChannelMap) -> CompiledSequence:
    errors = list(cross_validate_sequence(seq, chmap))
    if errors:
        raise CompileError(errors)

    safe_word = chmap.safe_word()
    warnings: list[str] = []

    main = _expand(seq.steps, chmap, safe_word, "steps", errors)
    abort = _expand(seq.abort, chmap, safe_word, "abort", errors)
    if errors:
        raise CompileError(errors)

    duration_ms = main[-1].t_ms if main else 0
    abort_duration_ms = abort[-1].t_ms if abort else 0
    max_duration_ms = round(seq.max_duration_s * 1000.0)
    if duration_ms > max_duration_ms:
        errors.append(f"duration {duration_ms} ms exceeds max_duration_s")

    # end-state safety
    if main and main[-1].dio_word != safe_word:
        if seq.allow_unsafe_end:
            warnings.append(
                f"sequence ends at 0x{main[-1].dio_word:04X}, not safe_word "
                f"0x{safe_word:04X} (allow_unsafe_end set)"
            )
        else:
            errors.append(
                f"final step word 0x{main[-1].dio_word:04X} != safe_word 0x{safe_word:04X} "
                "(set allow_unsafe_end: true to override)"
            )
    if abort and abort[-1].dio_word != safe_word:
        errors.append(
            f"abort table ends at 0x{abort[-1].dio_word:04X}, must equal safe_word "
            f"0x{safe_word:04X}"
        )

    # spark guard (D9)
    sparks = chmap.spark_channels()
    spark_mask = 0
    spark_guard_ms = 0
    for sc in sparks:
        spark_mask |= 1 << dio_number(sc.hw)
    if spark_mask:
        spark_guard_ms = min(sc.max_on_ms for sc in sparks)
        for what, steps, end in (("steps", main, duration_ms), ("abort", abort, abort_duration_ms)):
            on_ms = _max_spark_on_ms(steps, end, spark_mask)
            if on_ms > spark_guard_ms:
                errors.append(
                    f"{what}: spark on for {on_ms} ms in table, exceeds max_on_ms {spark_guard_ms}"
                )

    if errors:
        raise CompileError(errors)

    table = StepTable(
        dio_inhibit=chmap.dio_inhibit(),
        safe_word=safe_word,
        spark_guard_ms=spark_guard_ms,
        spark_mask=spark_mask,
        steps=tuple(main),
        abort=tuple(abort),
    )
    return CompiledSequence(seq.name, table, duration_ms, abort_duration_ms, warnings)
