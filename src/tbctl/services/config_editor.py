"""Config-editor apply path: validated writes of channels.yaml with backup
and in-place hot-apply (roadmap v2 ground rules).

Guarded by the API layer (sequencer IDLE, not recording, control off).
The write order is fail-safe: nothing is persisted unless the new map
validates AND every existing sequence still cross-validates against it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import yaml

from tbctl.config.loader import load_channels
from tbctl.config.models import ChannelMap, cross_validate_sequence

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
