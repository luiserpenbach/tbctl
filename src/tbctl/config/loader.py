"""YAML loading + validation + SHA256 hashing (spec D12).

Every loaded file's SHA256 (over raw bytes, exactly as on disk) is carried
alongside the parsed model so it can be stamped into run metadata.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from tbctl.config.models import ChannelMap, Dashboard, Sequence, cross_validate_sequence


class ConfigError(Exception):
    """A config file failed to load or validate. str() is human-readable."""


class _YamlLoader(yaml.SafeLoader):
    """SafeLoader minus YAML 1.1's on/off/yes/no booleans.

    Operators write `fail_safe: off` and `spark.igniter: on`; those must stay
    strings. Only true/false (any case) remain booleans.
    """


for _ch in "OoYyNn":
    if _ch in _YamlLoader.yaml_implicit_resolvers:
        _YamlLoader.yaml_implicit_resolvers[_ch] = [
            (tag, regexp)
            for tag, regexp in _YamlLoader.yaml_implicit_resolvers[_ch]
            if tag != "tag:yaml.org,2002:bool"
        ]


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


@dataclass
class ConfigBundle:
    channels: Loaded[ChannelMap]
    sequences: dict[str, Loaded[Sequence]] = field(default_factory=dict)
    dashboards: dict[str, Loaded[Dashboard]] = field(default_factory=dict)


def load_config_dir(config_dir: Path) -> ConfigBundle:
    """Load and validate an entire config directory; raise on the first
    channels.yaml problem, collect everything else."""
    channels = load_channels(config_dir / "channels.yaml")
    bundle = ConfigBundle(channels=channels)
    errors: list[str] = []
    for sub, loader, target in (
        ("sequences", load_sequence, bundle.sequences),
        ("dashboards", load_dashboard, bundle.dashboards),
    ):
        d = config_dir / sub
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml")):
            try:
                if loader is load_sequence:
                    loaded = load_sequence(path, channels.model)
                else:
                    loaded = loader(path)
                name = loaded.model.name
                if name in target:
                    errors.append(f"{path}: duplicate {sub[:-1]} name {name!r}")
                else:
                    target[name] = loaded
            except ConfigError as e:
                errors.append(str(e))
    if errors:
        raise ConfigError("\n".join(errors))
    return bundle
