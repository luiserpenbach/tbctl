"""Config-editor apply path: validated writes of channels.yaml with backup
and in-place hot-apply (roadmap v2 ground rules).

Guarded by the API layer (sequencer IDLE, not recording, control off).
The write order is fail-safe and transactional: nothing is persisted unless
the new config fully validates AND every existing sequence still compiles
against it; what is persisted is the validated model's canonical dump
(never the raw client payload), written atomically (temp file + rename);
and if the in-place hot-apply fails anyway, the previous file is restored
from its pre-write state and the old config re-applied before the error
propagates — a failed apply must never leave a new file on disk with the
services dead.
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

from tbctl.config.loader import (
    ConfigError,
    dump_yaml,
    load_channels,
    load_pid,
    load_sequence,
)
from tbctl.config.models import (
    ChannelMap,
    PidConfig,
    Sequence,
    cross_validate_dashboard,
    cross_validate_pid,
    cross_validate_review,
    dio_number,
)
from tbctl.services.compiler import CompiledSequence, CompileError, compile_sequence

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


def _atomic_write(path: Path, data: bytes | str) -> None:
    """Write a config file atomically: temp file in the same directory, then
    os.replace. A crash mid-write can never leave a truncated config."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        if isinstance(data, str):
            tmp.write_text(data)
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _restore_file(path: Path, prev: bytes | None) -> None:
    """Put a config file back to its pre-write state (delete if it did not
    exist before)."""
    if prev is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, prev)


def validate_channels_payload(ctx, payload: dict) -> tuple[ChannelMap, list[str]]:
    """Parse + cross-check an edited channel config. Returns (model, errors).

    Every existing sequence is fully COMPILED against the new map (not just
    cross-validated): compile-only breakage — e.g. a `fail_safe` flip that
    changes safe_word so every sequence stops compiling — must be caught at
    channel-edit time, not at the next /sequences/{name}/load.
    """
    chmap = ChannelMap.model_validate(payload)  # raises ValidationError -> 422
    errors: list[str] = []
    for name, loaded in ctx.bundle.sequences.items():
        try:
            compile_sequence(loaded.model, chmap)  # includes cross-validation
        except CompileError as e:
            errors.extend(f"sequence {name}: {msg}" for msg in e.errors)
    for name, loaded in ctx.bundle.pids.items():
        for msg in cross_validate_pid(loaded.model, chmap):
            errors.append(f"pid {name}: {msg}")
    for name, loaded in ctx.bundle.dashboards.items():
        for msg in cross_validate_dashboard(loaded.model, chmap, pid_names=ctx.bundle.pids.keys()):
            errors.append(f"dashboard {name}: {msg}")
    if ctx.bundle.review is not None:
        for msg in cross_validate_review(
            ctx.bundle.review.model, chmap, sequence_names=ctx.bundle.sequences.keys()
        ):
            errors.append(f"review: {msg}")
    return chmap, errors


async def apply_channels(ctx, chmap: ChannelMap, payload: dict) -> dict:
    """Write channels.yaml and hot-apply the new map across all services.

    `chmap` must come from validate_channels_payload (with no errors); the
    raw `payload` is ignored for persistence — the validated model's dump is
    what lands on disk. On any hot-apply failure the previous file is
    restored and the old map re-applied before the exception propagates.
    """
    del payload  # only the validated model is ever persisted
    path = ctx.bundle.channels.path
    old_loaded = ctx.bundle.channels
    prev_bytes = path.read_bytes() if path.is_file() else None
    backup = backup_file(path)
    _atomic_write(path, dump_yaml(chmap.model_dump(exclude_none=True)))
    new_loaded = load_channels(path)  # re-read for canonical sha256

    try:
        await ctx.acq.stop()
        ctx.bundle.channels = new_loaded
        ctx.device_mgr.update_chmap(new_loaded.model)
        ctx.device_mgr.rebuild_sim_backend()
        ctx.manual._chmap = new_loaded.model
        ctx.recorder._chmap = new_loaded.model
        ctx.acq.reconfigure(new_loaded.model)
        await ctx.acq.start()
    except Exception:
        # hot-apply failed: put the old file back and re-apply the old map so
        # the node is left running on the previous config, and the next
        # startup cannot crash on a half-applied file
        log.exception("channel hot-apply failed; restoring previous channels.yaml")
        _restore_file(path, prev_bytes)
        ctx.bundle.channels = old_loaded
        try:
            with suppress(Exception):
                await ctx.acq.stop()
            ctx.device_mgr.update_chmap(old_loaded.model)
            ctx.device_mgr.rebuild_sim_backend()
            ctx.manual._chmap = old_loaded.model
            ctx.recorder._chmap = old_loaded.model
            ctx.acq.reconfigure(old_loaded.model)
            await ctx.acq.start()
        except Exception:
            log.exception("failed to re-apply the previous channel map after apply failure")
        with suppress(Exception):
            ctx.events.log("channels_apply_failed", restored=True)
        raise

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
    and reload it into the bundle.

    Fully re-validates and compiles first (defense in depth even though the
    API layer has already compiled the payload), persists the validated
    model's dump atomically, and restores the previous file if the post-write
    reload fails for any reason.
    """
    # 1. validate fully before anything touches the disk
    seq, _compiled = compile_sequence_payload(ctx, payload)

    path = ctx.config_dir / "sequences" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_bytes = path.read_bytes() if path.is_file() else None
    backup = backup_file(path)
    # 2./3. write the validated model's dump, atomically
    _atomic_write(path, dump_yaml(seq.model_dump(exclude_none=True)))
    try:
        new_loaded = load_sequence(path, ctx.bundle.channels.model)
    except Exception:
        log.exception("sequence %s failed to reload after write; restoring previous file", name)
        _restore_file(path, prev_bytes)
        raise
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
# anything that could execute or embed foreign content. This is a strict
# allowlist sanitizer over the parsed XML tree — a blocklist regex is not a
# sanitizer (style-based CSS, entity tricks and external hrefs all walked
# straight past the old one).

SVG_MAX_BYTES = 2 * 1024 * 1024

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

# Static drawing elements only: no <style> (CSS can fetch external
# resources), no <script>, no <foreignObject>, no animation elements, no
# <image>, no <a>. <use> is allowed but only with a local '#fragment' href.
_SVG_ALLOWED_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "use",
        "symbol",
        "marker",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "desc",
    }
)

# Geometry + presentation attributes, plus id/class. No `style` attribute
# (inline CSS can reference external URLs), no event handlers, no hrefs
# (except the <use> special case handled explicitly).
_SVG_ALLOWED_ATTRS = frozenset(
    {
        "id",
        "class",
        "version",
        # geometry / layout
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "width",
        "height",
        "dx",
        "dy",
        "rotate",
        "viewBox",
        "preserveAspectRatio",
        "transform",
        "pathLength",
        "refX",
        "refY",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "orient",
        # paint / presentation
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-opacity",
        "opacity",
        "color",
        "display",
        "visibility",
        "vector-effect",
        "paint-order",
        "shape-rendering",
        "text-rendering",
        # text
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "letter-spacing",
        "word-spacing",
        "text-anchor",
        "text-decoration",
        "dominant-baseline",
        "alignment-baseline",
        "baseline-shift",
        # markers
        "marker-start",
        "marker-mid",
        "marker-end",
    }
)

_LOCAL_FRAGMENT_RE = re.compile(r"^#[A-Za-z_][A-Za-z0-9_\-.:]*$")
_URL_REF_RE = re.compile(r"url\(\s*['\"]?\s*([^'\")\s]*)", re.IGNORECASE)


def _localname(tag: str) -> tuple[str, str]:
    """Split an ElementTree tag/attr key into (namespace, localname)."""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _check_attr(elem_local: str, ns: str, local: str, value: str) -> str | None:
    """Return a problem string for a forbidden attribute, or None if safe."""
    if local.lower().startswith("on"):
        return f"event handler attribute {local!r}"
    if local == "href" and ns in ("", _XLINK_NS):
        # only <use href="#local-id"> is allowed
        if elem_local != "use":
            return f"href attribute on <{elem_local}>"
        if not _LOCAL_FRAGMENT_RE.match(value.strip()):
            return f"non-local use href {value!r} (only '#id' fragment refs allowed)"
        return None
    if ns == _XML_NS and local == "space":
        return None  # xml:space is harmless whitespace handling
    if ns not in ("", _SVG_NS):
        return f"foreign-namespace attribute {ns}:{local}"
    if local not in _SVG_ALLOWED_ATTRS:
        return f"attribute {local!r} not in the allowlist"
    for ref in _URL_REF_RE.findall(value):
        if not ref.startswith("#"):
            return f"non-local url() reference in {local!r}: {ref!r}"
    return None


def _sanitize_tree(root: ET.Element) -> tuple[ET.Element, list[str]]:
    """Walk the tree: strip everything outside the allowlist, collecting a
    problem string for each removal. Returns (cleaned root, problems)."""
    problems: list[str] = []

    def clean(elem: ET.Element) -> None:
        for child in list(elem):
            ns, local = _localname(child.tag) if isinstance(child.tag, str) else ("", "")
            if not isinstance(child.tag, str):
                # comments / processing instructions (only with custom parsers)
                elem.remove(child)
                continue
            if ns not in ("", _SVG_NS) or local not in _SVG_ALLOWED_ELEMENTS:
                problems.append(f"forbidden SVG element <{local or child.tag}>")
                elem.remove(child)
                continue
            clean(child)
        _, elem_local = _localname(elem.tag)
        for key in list(elem.attrib):
            a_ns, a_local = _localname(key)
            problem = _check_attr(elem_local, a_ns, a_local, elem.attrib[key])
            if problem is not None:
                problems.append(f"<{elem_local}>: {problem}")
                del elem.attrib[key]

    clean(root)
    return root, problems


def _parse_svg(content: str) -> tuple[ET.Element | None, list[str]]:
    problems: list[str] = []
    if len(content.encode()) > SVG_MAX_BYTES:
        problems.append(f"SVG larger than {SVG_MAX_BYTES // 1024} KiB")
    lowered = content.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        problems.append("DTD / entity declarations are not allowed")
    if problems:
        return None, problems
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        return None, [f"not well-formed XML: {e}"]
    ns, local = _localname(root.tag)
    if local != "svg" or ns not in ("", _SVG_NS):
        return None, ["not an SVG document (root element is not <svg>)"]
    return root, []


def sanitize_svg(content: str) -> list[str]:
    """Returns a list of problems; [] = safe to store as-is.

    Strict allowlist over the parsed XML: the stored bytes are exactly what
    the client sent, so anything the sanitizer would have to strip is instead
    reported as a problem (reject, never silently keep)."""
    root, problems = _parse_svg(content)
    if root is None:
        return problems
    _, walk_problems = _sanitize_tree(root)
    return walk_problems


def clean_svg(content: str) -> str:
    """Parse, strip everything outside the allowlist, and serialize the
    cleaned tree (namespace-normalized to a bare xmlns=svg root).

    Raises ConfigError if the document cannot be parsed as SVG at all."""
    root, problems = _parse_svg(content)
    if root is None:
        raise ConfigError("; ".join(problems))
    root, _dropped = _sanitize_tree(root)
    for elem in root.iter():
        if isinstance(elem.tag, str):
            elem.tag = _localname(elem.tag)[1]
        for key in list(elem.attrib):
            if _localname(key)[0]:  # only xml:space survives the walk; drop it
                del elem.attrib[key]
    root.set("xmlns", _SVG_NS)
    return ET.tostring(root, encoding="unicode")


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
    reload it into the bundle.

    Fully re-validates first (defense in depth even though the API layer has
    already validated the payload), persists the validated model's dump
    atomically, and restores the previous file if the post-write reload
    fails for any reason.
    """
    # 1. validate fully before anything touches the disk
    pid, errors = validate_pid_payload(ctx, payload)
    if errors:
        raise ConfigError("\n".join(errors))

    path = ctx.config_dir / "pid" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_bytes = path.read_bytes() if path.is_file() else None
    backup = backup_file(path)
    # 2./3. write the validated model's dump, atomically
    _atomic_write(path, dump_yaml(pid.model_dump(exclude_none=True)))
    try:
        new_loaded = load_pid(path, ctx.bundle.channels.model)
    except Exception:
        log.exception("pid %s failed to reload after write; restoring previous file", name)
        _restore_file(path, prev_bytes)
        raise
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
