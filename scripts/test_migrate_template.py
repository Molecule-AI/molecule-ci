"""Tests for migrate-template.py — pin the migration framework's
behavior so the FIRST real schema bump (the one that proves the system
end-to-end) doesn't have to discover semantics under deadline pressure.

The MIGRATIONS registry is empty today (we have only v1), so most
tests register a synthetic migration scoped to the test, exercise the
machinery, and unregister at teardown. This way the framework's
contract is locked in even before any real migration ships.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MIGRATOR_PATH = Path(__file__).resolve().parent / "migrate-template.py"


def _load_migrator():
    """Load migrate-template.py by path (its filename has a hyphen so
    we can't `import migrate-template` directly)."""
    spec = importlib.util.spec_from_file_location("migrator", MIGRATOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def migrator():
    """Fresh migrator module per test. Registry is global module
    state; tests that register synthetic migrations must clean up."""
    mod = _load_migrator()
    # Snapshot + restore MIGRATIONS so accidentally-leaked entries
    # from one test don't poison the next.
    snapshot = dict(mod.MIGRATIONS)
    yield mod
    mod.MIGRATIONS.clear()
    mod.MIGRATIONS.update(snapshot)


def _v1_template_config() -> dict:
    return {
        "name": "test-template",
        "runtime": "claude-code",
        "template_schema_version": 1,
        "description": "fixture",
        "tier": 1,
    }


# ─────────────────────────────────────── version detection

def test_detect_current_version_from_config(migrator):
    config = _v1_template_config()
    assert migrator._detect_current_version(config) == 1


def test_detect_missing_version_exits(migrator):
    config = {"name": "t", "runtime": "claude-code"}
    with pytest.raises(SystemExit) as exc:
        migrator._detect_current_version(config)
    assert "no `template_schema_version`" in str(exc.value)


def test_detect_non_int_version_exits(migrator):
    config = {"name": "t", "runtime": "claude-code", "template_schema_version": "1"}
    with pytest.raises(SystemExit) as exc:
        migrator._detect_current_version(config)
    assert "must be int" in str(exc.value)


# ─────────────────────────────────────── latest-version reachability

def test_latest_with_empty_registry_is_v1(migrator):
    """Floor case: every existing template is v1 even when no
    migrations are registered. Latest reachable = v1, so a no-op
    migration is the only valid action."""
    migrator.MIGRATIONS.clear()
    assert migrator._latest_known_version() == 1


def test_latest_with_one_migration_is_v2(migrator):
    """Adding a v1 → v2 migration moves the ceiling to v2. This is
    what happens the first time a real schema bump ships."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2}
    assert migrator._latest_known_version() == 2


def test_latest_chains_through_multiple_migrations(migrator):
    """Multi-step ceiling: v1 → v2 → v3 chain produces ceiling=3."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2}
    migrator.MIGRATIONS[2] = lambda c: {**c, "template_schema_version": 3}
    assert migrator._latest_known_version() == 3


# ─────────────────────────────────────── migrate_config core

def test_migrate_no_op_when_versions_match(migrator):
    """from == to → no migration step runs. Should not require any
    MIGRATIONS entry to be defined."""
    migrator.MIGRATIONS.clear()
    out = migrator.migrate_config(_v1_template_config(), 1, 1)
    assert out == _v1_template_config()
    assert out is not _v1_template_config()  # deep-copied, not aliased


def test_migrate_one_step_applies_function(migrator):
    """v1 → v2 with a registered migration produces the expected
    output and stamps the new version."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2, "added_in_v2": True}
    out = migrator.migrate_config(_v1_template_config(), 1, 2)
    assert out["template_schema_version"] == 2
    assert out["added_in_v2"] is True
    # Pre-existing keys preserved.
    assert out["name"] == "test-template"


def test_migrate_chains_v1_to_v3(migrator):
    """Two-step migration: v1 → v2 → v3. Each step applies in order."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2, "from_v1": True}
    migrator.MIGRATIONS[2] = lambda c: {**c, "template_schema_version": 3, "from_v2": True}
    out = migrator.migrate_config(_v1_template_config(), 1, 3)
    assert out["template_schema_version"] == 3
    assert out["from_v1"] is True
    assert out["from_v2"] is True


def test_migrate_missing_step_exits(migrator):
    """If MIGRATIONS lacks the v<current> step, fail loud rather than
    silently skip the version. Forward-only, never silent skip."""
    migrator.MIGRATIONS.clear()
    # No MIGRATIONS[1] registered.
    with pytest.raises(SystemExit) as exc:
        migrator.migrate_config(_v1_template_config(), 1, 2)
    assert "no migration registered for v1 → v2" in str(exc.value)


def test_migrate_backward_exits(migrator):
    """Schema versions are append-only. Asking for v2 → v1 must
    error, not silently downgrade."""
    migrator.MIGRATIONS.clear()
    config = {**_v1_template_config(), "template_schema_version": 2}
    with pytest.raises(SystemExit) as exc:
        migrator.migrate_config(config, 2, 1)
    assert "cannot migrate backward" in str(exc.value)


def test_migration_must_stamp_new_version(migrator):
    """A migration function that forgets to bump
    `template_schema_version` is a bug — catch it at apply time so
    the framework can never produce an inconsistent output."""
    migrator.MIGRATIONS.clear()
    # Buggy migration: doesn't update the version field.
    migrator.MIGRATIONS[1] = lambda c: {**c, "added_in_v2": True}
    with pytest.raises(SystemExit) as exc:
        migrator.migrate_config(_v1_template_config(), 1, 2)
    assert "did not stamp template_schema_version=2" in str(exc.value)


def test_migrate_does_not_mutate_input(migrator):
    """migrate_config returns a fresh dict; the caller's input is
    untouched. Pin this so a shared-state migration can't accidentally
    poison the caller's view of the original template."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2}
    original = _v1_template_config()
    snapshot = dict(original)
    _ = migrator.migrate_config(original, 1, 2)
    assert original == snapshot


# ─────────────────────────────────────── CLI smoke

def test_cli_writes_migrated_yaml(migrator, tmp_path):
    """End-to-end: --to migrates the file in place and exits 0."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2, "added": "v2-marker"}

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
    )
    rc = migrator.main([str(tmp_path), "--to", "2"])
    assert rc == 0
    written = cfg.read_text()
    assert "template_schema_version: 2" in written
    assert "added: v2-marker" in written


def test_cli_dry_run_does_not_modify_file(migrator, tmp_path, capsys):
    """--dry-run prints the migrated YAML to stdout but leaves the
    on-disk file untouched."""
    migrator.MIGRATIONS.clear()
    migrator.MIGRATIONS[1] = lambda c: {**c, "template_schema_version": 2}

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
    )
    original_disk = cfg.read_text()
    rc = migrator.main([str(tmp_path), "--to", "2", "--dry-run"])
    assert rc == 0
    assert cfg.read_text() == original_disk  # untouched

    captured = capsys.readouterr()
    assert "template_schema_version: 2" in captured.out


def test_cli_no_op_when_already_at_target(migrator, tmp_path, capsys):
    """If the template is already at the target version, exit 0
    without modifying the file. Not an error — common when running
    the migration script defensively in CI."""
    migrator.MIGRATIONS.clear()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: t\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
    )
    original = cfg.read_text()
    rc = migrator.main([str(tmp_path), "--to", "1"])
    assert rc == 0
    assert cfg.read_text() == original


def test_cli_missing_config_exits(migrator, tmp_path):
    """If the target dir has no config.yaml, error clearly rather
    than try to apply migrations to nothing."""
    with pytest.raises(SystemExit) as exc:
        migrator.main([str(tmp_path), "--to", "2"])
    assert "config.yaml" in str(exc.value) and "does not exist" in str(exc.value)
