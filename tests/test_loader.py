"""Loader: YAML semantics, SHA256 stamping, config-dir aggregation."""

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

from tbctl.config.loader import (
    ConfigError,
    dump_yaml,
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


# ------------------------------------------------------------- YAML hygiene


def test_importing_loader_does_not_mutate_global_yaml():
    """M17: the custom resolver table must be a copy — importing tbctl must
    never change yaml.safe_load / yaml.safe_dump for the rest of the process."""
    import tbctl.config.loader  # noqa: F401  (the import IS the test setup)

    assert yaml.safe_load("a: on") == {"a": True}
    assert yaml.safe_load("b: off") == {"b": False}
    assert yaml.safe_load("c: yes") == {"c": True}
    # and safe_dump still quotes bool-like strings for YAML 1.1 consumers
    assert yaml.safe_load(yaml.safe_dump({"x": "off"})) == {"x": "off"}


def test_dump_yaml_quotes_bool_like_strings():
    """M17 (dump side): files we write must stay unambiguous for vanilla
    YAML tools — `fail_safe: off` would read back as False."""
    out = dump_yaml({"fail_safe": "off", "step": "on", "really": True, "note": "yes"})
    assert "'off'" in out and "'on'" in out and "'yes'" in out
    parsed = yaml.safe_load(out)
    assert parsed == {"fail_safe": "off", "step": "on", "really": True, "note": "yes"}


def test_duplicate_yaml_keys_rejected(tmp_path):
    """M18: silent last-wins on duplicate keys is unacceptable in a
    fire-control format."""
    p = tmp_path / "seq.yaml"
    p.write_text(
        "name: s\nmax_duration_s: 5.0\n"
        "steps:\n  - {t: 0.0, set: {valve.a: open, valve.a: close}}\n"
        "abort:\n  - {t: 0.0, set: {valve.a: close}}\n"
    )
    with pytest.raises(ConfigError, match="duplicate key"):
        load_sequence(p)
    # duplicated top-level block (a repeated steps: table) is also an error
    p.write_text(
        "name: s\nmax_duration_s: 5.0\n"
        "steps: [{t: 0.0, set: {valve.a: open}}]\n"
        "steps: [{t: 1.0, set: {valve.a: close}}]\n"
        "abort: [{t: 0.0, set: {valve.a: close}}]\n"
    )
    with pytest.raises(ConfigError, match="duplicate key 'steps'"):
        load_sequence(p)


# ------------------------------------------------------- config-dir checks


def _copy_configs(tmp_path: Path) -> Path:
    cfg = tmp_path / "configs"
    shutil.copytree(CONFIGS, cfg)
    return cfg


def test_yml_extension_also_loaded(tmp_path):
    """L17: *.yml files must not be silently ignored."""
    cfg = _copy_configs(tmp_path)
    (cfg / "sequences" / "coldflow_v1.yaml").rename(cfg / "sequences" / "coldflow_v1.yml")
    bundle = load_config_dir(cfg)
    assert "coldflow_v1" in bundle.sequences


def test_load_config_dir_compiles_sequences(tmp_path):
    """M15: compile-only errors (final state != safe word) must fail
    validation/startup, not the first /load on test day."""
    cfg = _copy_configs(tmp_path)
    seq_path = cfg / "sequences" / "coldflow_v1.yaml"
    text = seq_path.read_text()
    # cut the final purge-close step: the sequence now ends unsafely
    assert "- {t: 8.200, set: {valve.purge: close}}" in text
    seq_path.write_text(text.replace("  - {t: 8.200, set: {valve.purge: close}}\n", ""))
    with pytest.raises(ConfigError, match="safe_word"):
        load_config_dir(cfg)
    from tbctl.cli import main

    assert main(["validate", "--config-dir", str(cfg)]) == 1


def test_load_config_dir_checks_review_sequence_scope(tmp_path):
    """M20: review rules scoped to a nonexistent sequence are dead rules."""
    cfg = _copy_configs(tmp_path)
    review = cfg / "review.yaml"
    review.write_text(
        review.read_text().replace("sequences: [hotfire_v1]", "sequences: [renamed_away]")
    )
    with pytest.raises(ConfigError, match="unknown sequence 'renamed_away'"):
        load_config_dir(cfg)


def test_load_config_dir_checks_dashboards(tmp_path):
    """L17: dashboards referencing unknown tags / pid configs must fail."""
    cfg = _copy_configs(tmp_path)
    dash = cfg / "dashboards" / "hotfire_main.yaml"
    dash.write_text(dash.read_text().replace("pressure.chamber", "pressure.gone"))
    with pytest.raises(ConfigError, match=r"pressure\.gone"):
        load_config_dir(cfg)

    cfg2 = _copy_configs(tmp_path / "second")
    (cfg2 / "dashboards" / "pid_dash.yaml").write_text(
        "name: pid_dash\npanels:\n"
        "  - {id: p1, type: pid, x: 0, y: 0, w: 4, h: 4, pid: no_such_pid}\n"
    )
    with pytest.raises(ConfigError, match="unknown pid config 'no_such_pid'"):
        load_config_dir(cfg2)
