"""Tests for validate-workspace-template.py — pin the drift contract.

Each test materialises a tiny template directory in a tmpdir, runs the
validator's check functions in-process, and asserts on the captured
ERRORS / WARNINGS lists. The 8 template repos in the wild are the
ground-truth integration test (CI runs this validator against each on
push), but those repos can change at any time. These tests pin the
contract itself so a refactor of the validator can't silently weaken
it.

Important: the validator was chosen to be import-safe (no top-level
side effects), so the test patches the cwd via os.chdir into tmpdirs.
The module's ERRORS/WARNINGS lists are reset at the start of each
test via _reset_validator_state().
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


VALIDATOR_PATH = Path(__file__).resolve().parent / "validate-workspace-template.py"


def _load_validator():
    """Load the validator module by path (its filename has a hyphen so
    we can't `import validate-workspace-template` directly)."""
    spec = importlib.util.spec_from_file_location("validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def validator(monkeypatch):
    """Fresh validator module per test, cwd pinned to tmpdir below."""
    mod = _load_validator()
    mod.ERRORS.clear()
    mod.WARNINGS.clear()
    return mod


def _good_dockerfile() -> str:
    """Canonical Dockerfile that should pass every check."""
    return (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        'RUN pip install -r requirements.txt && \\\n'
        '    if [ -n "${RUNTIME_VERSION}" ]; then \\\n'
        '      pip install --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \\\n'
        '    fi\n'
        'ENTRYPOINT ["molecule-runtime"]\n'
    )


def _good_config_yaml() -> str:
    return (
        "name: test-template\n"
        "runtime: claude-code\n"
        "template_schema_version: 1\n"
        "description: A test template\n"
        "tier: 1\n"
    )


def _good_requirements_txt() -> str:
    return "molecule-ai-workspace-runtime>=0.1.0\n"


def _materialise(tmp_path: Path, dockerfile: str | None = None,
                 config_yaml: str | None = None,
                 requirements: str | None = None,
                 adapter_py: str | None = None) -> None:
    if dockerfile is not None:
        (tmp_path / "Dockerfile").write_text(dockerfile)
    if config_yaml is not None:
        (tmp_path / "config.yaml").write_text(config_yaml)
    if requirements is not None:
        (tmp_path / "requirements.txt").write_text(requirements)
    if adapter_py is not None:
        (tmp_path / "adapter.py").write_text(adapter_py)


# ───────────────────────────────────────────────────────── happy paths

def test_canonical_template_passes(validator, tmp_path, monkeypatch):
    _materialise(
        tmp_path,
        dockerfile=_good_dockerfile(),
        config_yaml=_good_config_yaml(),
        requirements=_good_requirements_txt(),
    )
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    validator.check_config_yaml()
    validator.check_requirements()
    validator.check_adapter()
    assert validator.ERRORS == [], validator.ERRORS


def test_custom_entrypoint_script_passes_when_it_execs_runtime(validator, tmp_path, monkeypatch):
    """claude-code style: ENTRYPOINT [/entrypoint.sh] + entrypoint.sh
    that exec's molecule-runtime at the end. Must pass."""
    df = (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "COPY requirements.txt .\n"
        'RUN pip install -r requirements.txt && \\\n'
        '    if [ -n "${RUNTIME_VERSION}" ]; then \\\n'
        '      pip install --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; \\\n'
        '    fi\n'
        "COPY entrypoint.sh /entrypoint.sh\n"
        'ENTRYPOINT ["/entrypoint.sh"]\n'
    )
    ep = (
        "#!/bin/sh\n"
        "set -e\n"
        '# drop privileges then exec the runtime\n'
        'exec gosu agent molecule-runtime "$@"\n'
    )
    _materialise(
        tmp_path,
        dockerfile=df,
        config_yaml=_good_config_yaml(),
        requirements=_good_requirements_txt(),
    )
    (tmp_path / "entrypoint.sh").write_text(ep)
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert validator.ERRORS == [], validator.ERRORS


# ───────────────────────────────────────────────────────── Dockerfile drift

def test_wrong_base_image_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace("python:3.11-slim", "python:3.10-alpine")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("FROM python:3.11-slim" in e for e in validator.ERRORS)


def test_missing_arg_runtime_version_errors(validator, tmp_path, monkeypatch):
    """Without ARG RUNTIME_VERSION, the cascade rebuild silently ships
    the previous runtime — the cache trap that bit us 5x on 2026-04-27."""
    df = _good_dockerfile().replace("ARG RUNTIME_VERSION=\n", "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("ARG RUNTIME_VERSION" in e for e in validator.ERRORS)


def test_missing_runtime_version_in_run_block_errors(validator, tmp_path, monkeypatch):
    """ARG declared but NEVER referenced in a RUN — same cache-trap,
    different shape. Pin both."""
    df = (
        "FROM python:3.11-slim\n"
        "ARG RUNTIME_VERSION=\n"
        "RUN useradd -u 1000 -m -s /bin/bash agent\n"
        "RUN pip install molecule-ai-workspace-runtime\n"
        'ENTRYPOINT ["molecule-runtime"]\n'
    )
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("RUNTIME_VERSION" in e and "RUN block" in e for e in validator.ERRORS)


def test_missing_agent_user_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace("RUN useradd -u 1000 -m -s /bin/bash agent\n", "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("agent" in e for e in validator.ERRORS)


def test_missing_entrypoint_errors(validator, tmp_path, monkeypatch):
    df = _good_dockerfile().replace('ENTRYPOINT ["molecule-runtime"]\n', "")
    _materialise(tmp_path, dockerfile=df, config_yaml=_good_config_yaml(),
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_dockerfile()
    assert any("molecule-runtime" in e and ("ENTRYPOINT" in e or "entrypoint" in e)
               for e in validator.ERRORS)


# ───────────────────────────────────────────────────────── config.yaml drift

def test_missing_required_keys_errors(validator, tmp_path, monkeypatch):
    cfg = "description: only description, no name/runtime/version\n"
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    missing_msgs = [e for e in validator.ERRORS if "missing required key" in e]
    assert len(missing_msgs) >= 3  # name, runtime, template_schema_version


def test_string_template_schema_version_errors(validator, tmp_path, monkeypatch):
    cfg = (
        "name: t\n"
        "runtime: claude-code\n"
        'template_schema_version: "1"\n'  # str, not int
    )
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("template_schema_version must be int" in e for e in validator.ERRORS)


def test_unknown_runtime_warns_not_errors(validator, tmp_path, monkeypatch):
    cfg = _good_config_yaml().replace("claude-code", "my-experimental-runtime")
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("not in known set" in w for w in validator.WARNINGS)
    assert validator.ERRORS == []  # custom runtimes are allowed


def test_unknown_top_level_keys_warn(validator, tmp_path, monkeypatch):
    cfg = _good_config_yaml() + "weird_drift_key: something\n"
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=cfg,
                 requirements=_good_requirements_txt())
    monkeypatch.chdir(tmp_path)
    validator.check_config_yaml()
    assert any("unknown top-level keys" in w and "weird_drift_key" in w
               for w in validator.WARNINGS)


# ───────────────────────────────────────────────────────── requirements.txt

def test_missing_runtime_in_requirements_errors(validator, tmp_path, monkeypatch):
    _materialise(tmp_path, dockerfile=_good_dockerfile(), config_yaml=_good_config_yaml(),
                 requirements="fastapi\n")
    monkeypatch.chdir(tmp_path)
    validator.check_requirements()
    assert any("molecule-ai-workspace-runtime" in e for e in validator.ERRORS)


# ───────────────────────────────────────────────────────── adapter.py

def test_legacy_molecule_ai_import_warns(validator, tmp_path, monkeypatch):
    """Pre-#87 package was named differently. Catch any laggards."""
    adapter = "from molecule_ai.adapter_base import BaseAdapter\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter()
    assert any("molecule_ai" in w for w in validator.WARNINGS)


def test_modern_molecule_runtime_import_does_not_warn(validator, tmp_path, monkeypatch):
    """Regression cover: the original validator's warning ('don't import
    molecule_runtime') was BACKWARDS — that's the canonical name now.
    Pin that the new validator does NOT emit a false positive."""
    adapter = "from molecule_runtime.adapter_base import BaseAdapter\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter()
    legacy_warnings = [w for w in validator.WARNINGS if "molecule_ai" in w]
    assert legacy_warnings == [], legacy_warnings
