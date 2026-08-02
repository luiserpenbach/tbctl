"""Config-editor apply path: validated writes of channels.yaml with backup
and in-place hot-apply (roadmap v2 ground rules).

Guarded by the API layer (sequencer IDLE, not recording, control off).
The write order is fail-safe: nothing is persisted unless the new map
validates AND every existing sequence still cross-validates against it.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import yaml

from tbctl.config.loader import load_channels, load_pid, load_sequence
from tbctl.config.models import (
    ChannelMap,
    PidConfig,
    Sequence,
    cross_validate_pid,
    cross_validate_sequence,
    dio_number,
)
from tbctl.services.compiler import CompiledSequence, compile_sequence

log = logging.getLogger(__name__)

BACKUP_KEEP = 20


def backup_file(path: Path) -> Path | None:
    """Snapshot a config file into configs/.backups/, pruning old copies."""
    if not path.is_file():
        return None
    # configs/<file>.yaml -> configs/.backups ; configs/<sub>/<file>.yaml too
    root = path.parent
    if root.name in ("dashboards", "sequences", "pid", "checklists"):
        root = root.parent
    backups = root / ".backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = backups / f"{path.stem}.{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    old = sorted(backups.glob(f"{path.stem}.*{path.suffix}"))
    for stale in old[:-BACKUP_KEEP]:
        stale.unlink(missing_ok=True)
    return dest


def validate_channels_payload(ctx, payload: dict) -> tuple[ChannelMap, list[str]]:
    """Parse + cross-check an edited channel config. Returns (model, errors)."""
    chmap = ChannelMap.model_validate(payload)  # raises ValidationError -> 422
    errors: list[str] = []
    for name, loaded in ctx.bundle.sequences.items():
        for msg in cross_validate_sequence(loaded.model, chmap):
            errors.append(f"sequence {name}: {msg}")
    for name, loaded in ctx.bundle.pids.items():
        for msg in cross_validate_pid(loaded.model, chmap):
            errors.append(f"pid {name}: {msg}")
    return chmap, errors


async def apply_channels(ctx, chmap: ChannelMap, payload: dict) -> dict:
    """Write channels.yaml and hot-apply the new map across all services."""
    path = ctx.bundle.channels.path
    backup = backup_file(path)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    new_loaded = load_channels(path)  # re-read for canonical sha256

    await ctx.acq.stop()
    ctx.bundle.channels = new_loaded
    ctx.device_mgr.update_chmap(new_loaded.model)
    ctx.device_mgr.rebuild_sim_backend()
    ctx.manual._chmap = new_loaded.model
    ctx.recorder._chmap = new_loaded.model
    ctx.acq.reconfigure(new_loaded.model)
    await ctx.acq.start()

    ctx.events.log(
        "channels_saved",
        sha256=new_loaded.sha256[:16],
        backup=str(backup) if backup else None,
        channels=len(new_loaded.model.channels),
    )
    log.info(
        "channels.yaml applied (%d channels), stream restarted", len(new_loaded.model.channels)
    )
    return {"ok": True, "sha256": new_loaded.sha256, "backup": str(backup) if backup else None}


# ---------------------------------------------------------------------------
# sequence editor (roadmap v2 phase 12)
# ---------------------------------------------------------------------------


def compile_sequence_payload(ctx, payload: dict) -> tuple[Sequence, CompiledSequence]:
    """Parse + compile an edited sequence against the live channel map.

    Raises pydantic.ValidationError (model errors) or CompileError
    (cross-validation / word-expansion errors) — the API maps both to 422.
    """
    seq = Sequence.model_validate(payload)
    compiled = compile_sequence(seq, ctx.bundle.channels.model)
    return seq, compiled


def sequence_timeline(ctx, compiled: CompiledSequence) -> dict:
    """Compiled table -> per-output lane data for the editor's preview.

    What the preview shows is literally the words that fire: full-state
    carry-forward words (D7), not the sparse YAML rows.
    """
    chmap = ctx.bundle.channels.model
    return {
        "outputs": [
            {
                "tag": c.tag,
                "bit": dio_number(c.hw),
                "kind": c.kind,
                "active_level": chmap.active_level(c),
            }
            for c in chmap.outputs()
        ],
        "safe_word": compiled.table.safe_word,
        "duration_ms": compiled.duration_ms,
        "abort_duration_ms": compiled.abort_duration_ms,
        "steps": [{"t_ms": s.t_ms, "word": s.dio_word} for s in compiled.table.steps],
        "abort": [{"t_ms": s.t_ms, "word": s.dio_word} for s in compiled.table.abort],
    }


def apply_sequence(ctx, name: str, payload: dict) -> dict:
    """Write configs/sequences/<name>.yaml (backing up any existing copy)
    and reload it into the bundle. Caller has already compiled the payload."""
    path = ctx.config_dir / "sequences" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_file(path)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    new_loaded = load_sequence(path, ctx.bundle.channels.model)
    ctx.bundle.sequences[name] = new_loaded

    ctx.events.log(
        "sequence_saved",
        sequence=name,
        sha256=new_loaded.sha256[:16],
        backup=str(backup) if backup else None,
    )
    log.info("sequence %s saved (%d steps)", name, len(new_loaded.model.steps))
    return {"ok": True, "sha256": new_loaded.sha256, "backup": str(backup) if backup else None}


# ---------------------------------------------------------------------------
# P&ID editor (roadmap v2 phase 13)
# ---------------------------------------------------------------------------

# Uploaded backgrounds are rendered inline in the operator client, so refuse
# anything that could execute or embed foreign content.
_SVG_FORBIDDEN = re.compile(
    r"<\s*script|<\s*foreignObject|<\s*iframe|<\s*embed|<\s*object"
    r"|\son[a-z]+\s*=|javascript:",
    re.IGNORECASE,
)
SVG_MAX_BYTES = 2 * 1024 * 1024


def sanitize_svg(content: str) -> list[str]:
    """Returns a list of problems; [] = safe to store."""
    problems = []
    if len(content.encode()) > SVG_MAX_BYTES:
        problems.append(f"SVG larger than {SVG_MAX_BYTES // 1024} KiB")
    if "<svg" not in content[:4096].lower():
        problems.append("not an SVG document (no <svg> root)")
    if m := _SVG_FORBIDDEN.search(content):
        problems.append(f"forbidden SVG content: {m.group(0).strip()!r}")
    return problems


def validate_pid_payload(ctx, payload: dict) -> tuple[PidConfig, list[str]]:
    """Parse + cross-check an edited P&ID. Returns (model, errors)."""
    pid = PidConfig.model_validate(payload)  # raises ValidationError -> 422
    errors = cross_validate_pid(pid, ctx.bundle.channels.model)
    if pid.background is not None:
        asset = ctx.config_dir / "pid" / "assets" / pid.background
        if not asset.is_file():
            errors.append(f"background asset {pid.background!r} is not uploaded")
    return pid, errors


def apply_pid(ctx, name: str, payload: dict) -> dict:
    """Write configs/pid/<name>.yaml (backing up any existing copy) and
    reload it into the bundle. Caller has already validated the payload."""
    path = ctx.config_dir / "pid" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_file(path)
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    new_loaded = load_pid(path, ctx.bundle.channels.model)
    ctx.bundle.pids[name] = new_loaded

    ctx.events.log(
        "pid_saved",
        pid=name,
        sha256=new_loaded.sha256[:16],
        backup=str(backup) if backup else None,
        elements=len(new_loaded.model.elements),
    )
    log.info("pid %s saved (%d elements)", name, len(new_loaded.model.elements))
    return {"ok": True, "sha256": new_loaded.sha256, "backup": str(backup) if backup else None}
