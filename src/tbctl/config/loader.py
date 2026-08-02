"""YAML loading + validation + SHA256 hashing (spec D12).

Every loaded file's SHA256 (over raw bytes, exactly as on disk) is carried
alongside the parsed model so it can be stamped into run metadata.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from tbctl.config.models import (
    ChannelMap,
    Dashboard,
    PidConfig,
    ReviewConfig,
    Sequence,
    cross_validate_dashboard,
    cross_validate_pid,
    cross_validate_review,
    cross_validate_sequence,
)


class ConfigError(Exception):
    """A config file failed to load or validate. str() is human-readable."""


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader minus YAML 1.1's on/off/yes/no booleans, plus hard
    duplicate-key errors.

    Operators write `fail_safe: off` and `spark.igniter: on`; those must stay
    strings. Only true/false (any case) remain booleans.

    Duplicate mapping keys are a hard error: YAML's silent last-wins would let
    `set: {valve.a: open, valve.a: close}` load as `close` — unacceptable in a
    fire-control format.
    """

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
        seen: set = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=True)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        f"found duplicate key {key!r}",
                        key_node.start_mark,
                    )
                seen.add(key)
            except TypeError:
                # unhashable key: the parent constructor raises the real error
                pass
        return super().construct_mapping(node, deep)


# Copy the inherited resolver table into this subclass BEFORE filtering it.
# The table is a plain dict defined on yaml.resolver.Resolver; mutating it in
# place would change yaml.safe_load / yaml.safe_dump behavior for every
# library in the process (M17).
_YamlLoader.yaml_implicit_resolvers = {
    _key: list(_val) for _key, _val in _YamlLoader.yaml_implicit_resolvers.items()
}
for _ch in "OoYyNn":
    if _ch in _YamlLoader.yaml_implicit_resolvers:
        _YamlLoader.yaml_implicit_resolvers[_ch] = [
            (tag, regexp)
            for tag, regexp in _YamlLoader.yaml_implicit_resolvers[_ch]
            if tag != "tag:yaml.org,2002:bool"
        ]


# All YAML 1.1 bool-like plain scalars. Anything we WRITE that matches this
# must be quoted, or vanilla YAML consumers read `fail_safe: off` as False.
_YAML11_BOOL_RE = re.compile(
    r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO"
    r"|true|True|TRUE|false|False|FALSE"
    r"|on|On|ON|off|Off|OFF)$"
)


class _YamlDumper(yaml.SafeDumper):
    """SafeDumper that single-quotes YAML 1.1 bool-like strings (on/off/
    yes/no/...), keeping written config files unambiguous for standard YAML
    tooling regardless of in-process resolver state."""


def _repr_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "'" if _YAML11_BOOL_RE.match(data) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_YamlDumper.add_representer(str, _repr_str)


def dump_yaml(data: dict) -> str:
    """Canonical YAML serialization for config files written by the editor."""
    return yaml.dump(data, Dumper=_YamlDumper, sort_keys=False)


@dataclass(frozen=True)
class Loaded[T]:
    model: T
    sha256: str
    path: Path


def _read_yaml(path: Path) -> tuple[dict, str]:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from e
    sha = hashlib.sha256(raw).hexdigest()
    try:
        data = yaml.load(raw, Loader=_YamlLoader)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: YAML parse error: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data, sha


def _format_validation_error(path: Path, e: ValidationError) -> str:
    lines = [f"{path}: {e.error_count()} validation error(s)"]
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_channels(path: Path) -> Loaded[ChannelMap]:
    data, sha = _read_yaml(path)
    try:
        return Loaded(ChannelMap.model_validate(data), sha, path)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e


def load_sequence(path: Path, chmap: ChannelMap | None = None) -> Loaded[Sequence]:
    data, sha = _read_yaml(path)
    try:
        seq = Sequence.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e
    if chmap is not None and (errors := cross_validate_sequence(seq, chmap)):
        raise ConfigError("\n".join(f"{path}: {msg}" for msg in errors))
    return Loaded(seq, sha, path)


def load_dashboard(path: Path) -> Loaded[Dashboard]:
    data, sha = _read_yaml(path)
    try:
        return Loaded(Dashboard.model_validate(data), sha, path)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e


def load_pid(path: Path, chmap: ChannelMap | None = None) -> Loaded[PidConfig]:
    data, sha = _read_yaml(path)
    try:
        pid = PidConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e
    if chmap is not None and (errors := cross_validate_pid(pid, chmap)):
        raise ConfigError("\n".join(f"{path}: {msg}" for msg in errors))
    return Loaded(pid, sha, path)


def load_review(
    path: Path,
    chmap: ChannelMap | None = None,
    sequence_names: Collection[str] | None = None,
) -> Loaded[ReviewConfig]:
    data, sha = _read_yaml(path)
    try:
        cfg = ReviewConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(path, e)) from e
    if chmap is not None and (errors := cross_validate_review(cfg, chmap, sequence_names)):
        raise ConfigError("\n".join(f"{path}: {msg}" for msg in errors))
    return Loaded(cfg, sha, path)


@dataclass
class ConfigBundle:
    channels: Loaded[ChannelMap]
    sequences: dict[str, Loaded[Sequence]] = field(default_factory=dict)
    dashboards: dict[str, Loaded[Dashboard]] = field(default_factory=dict)
    pids: dict[str, Loaded[PidConfig]] = field(default_factory=dict)
    review: Loaded[ReviewConfig] | None = None


def _config_files(d: Path) -> list[Path]:
    return sorted(itertools.chain(d.glob("*.yaml"), d.glob("*.yml")))


def load_config_dir(config_dir: Path) -> ConfigBundle:
    """Load and validate an entire config directory; raise on the first
    channels.yaml problem, collect everything else.

    Beyond per-file validation this also COMPILES every sequence against the
    channel map (so compile-only rules — final state == safe word, abort ends
    safe, spark on-time, step spacing at ms resolution — fail here, not on
    test day), cross-validates dashboards against the channel map and P&ID
    set, and checks review-rule `sequences:` scoping against the sequences
    that actually exist.
    """
    channels = load_channels(config_dir / "channels.yaml")
    bundle = ConfigBundle(channels=channels)
    errors: list[str] = []
    for sub, loader, target in (
        ("sequences", load_sequence, bundle.sequences),
        ("dashboards", load_dashboard, bundle.dashboards),
        ("pid", load_pid, bundle.pids),
    ):
        d = config_dir / sub
        if not d.is_dir():
            continue
        for path in _config_files(d):
            try:
                if loader in (load_sequence, load_pid):
                    loaded = loader(path, channels.model)
                else:
                    loaded = loader(path)
                name = loaded.model.name
                if name in target:
                    errors.append(f"{path}: duplicate {sub.rstrip('s')} name {name!r}")
                else:
                    target[name] = loaded
            except ConfigError as e:
                errors.append(str(e))

    # compile every sequence: compile-only errors must surface here (M15),
    # never first at /sequences/{name}/load on test day
    from tbctl.services.compiler import CompileError, compile_sequence

    for _name, loaded_seq in sorted(bundle.sequences.items()):
        try:
            compile_sequence(loaded_seq.model, channels.model)
        except CompileError as e:
            errors.extend(f"{loaded_seq.path}: {msg}" for msg in e.errors)

    for _name, loaded_dash in sorted(bundle.dashboards.items()):
        for msg in cross_validate_dashboard(
            loaded_dash.model, channels.model, pid_names=bundle.pids.keys()
        ):
            errors.append(f"{loaded_dash.path}: {msg}")

    review_path = config_dir / "review.yaml"
    if review_path.is_file():
        try:
            bundle.review = load_review(
                review_path, channels.model, sequence_names=bundle.sequences.keys()
            )
        except ConfigError as e:
            errors.append(str(e))
    if errors:
        raise ConfigError("\n".join(errors))
    return bundle
