"""Loader: YAML semantics, SHA256 stamping, config-dir aggregation."""

import hashlib
from pathlib import Path

import pytest

from tbctl.config.loader import (
    ConfigError,
    load_channels,
    load_config_dir,
    load_sequence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"


def test_yaml_on_off_stay_strings(tmp_path):
    """YAML 1.1 would parse bare on/off as booleans; ours must not."""
    p = tmp_path / "seq.yaml"
    p.write_text(
        "name: s\nmax_duration_s: 5.0\n"
        "steps:\n  - {t: 0.0, set: {spark.igniter: on}}\n"
        "  - {t: 1.0, set: {spark.igniter: off}}\n"
        "abort:\n  - {t: 0.0, set: {spark.igniter: off}}\n"
    )
    seq = load_sequence(p).model
    assert seq.steps[0].set["spark.igniter"] == "on"
    assert seq.steps[1].set["spark.igniter"] == "off"


def test_true_false_still_booleans(tmp_path):
    p = tmp_path / "seq.yaml"
    p.write_text(
        "name: s\nmax_duration_s: 5.0\nallow_unsafe_end: true\n"
        "steps: [{t: 0.0, set: {a.b: open}}]\n"
        "abort: [{t: 0.0, set: {a.b: close}}]\n"
    )
    assert load_sequence(p).model.allow_unsafe_end is True


def test_sha256_matches_disk_bytes():
    path = CONFIGS / "channels.yaml"
    loaded = load_channels(path)
    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_example_config_dir_loads():
    bundle = load_config_dir(CONFIGS)
    assert "hotfire_v1" in bundle.sequences
    assert "coldflow_v1" in bundle.sequences
    assert "hotfire_main" in bundle.dashboards
    assert bundle.channels.model.safe_word() is not None


def test_bad_yaml_reports_path(tmp_path):
    p = tmp_path / "channels.yaml"
    p.write_text("device: [unclosed")
    with pytest.raises(ConfigError, match=r"channels\.yaml"):
        load_channels(p)


def test_sequence_cross_validation_failure(tmp_path):
    chmap = load_channels(CONFIGS / "channels.yaml").model
    p = tmp_path / "seq.yaml"
    p.write_text(
        "name: bad\nmax_duration_s: 5.0\n"
        "steps: [{t: 0.0, set: {valve.does_not_exist: open}}]\n"
        "abort: [{t: 0.0, set: {valve.ox_main: close}}]\n"
    )
    with pytest.raises(ConfigError, match="not an output channel"):
        load_sequence(p, chmap)


def test_cli_validate_examples():
    from tbctl.cli import main

    assert main(["validate", "--config-dir", str(CONFIGS)]) == 0
