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


# ──────────────────── adapter.py runtime-load (strong contract)
#
# These tests pin the contract that adapter.py must be importable AND
# define at least one BaseAdapter subclass — the same path the runtime
# uses at workspace boot. Skipped when molecule-ai-workspace-runtime
# isn't installed in the test environment (the validator's CI workflow
# guarantees it via `pip install -r requirements.txt` before invoking
# the validator; local pytest can run with or without it).

def _has_runtime_installed() -> bool:
    """True if molecule-ai-workspace-runtime is importable. Used to skip
    the runtime-load tests when running pytest locally without the
    runtime in the venv."""
    try:
        import molecule_runtime.adapters.base  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


_RUNTIME_AVAILABLE = _has_runtime_installed()
_skip_no_runtime = pytest.mark.skipif(
    not _RUNTIME_AVAILABLE,
    reason="molecule-ai-workspace-runtime not installed in test env",
)


def test_no_adapter_skips_runtime_load_silently(validator, tmp_path, monkeypatch):
    """No adapter.py = use default langgraph executor from the wheel.
    That's policy, not drift, so runtime-load check should not fire."""
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    # No ERRORS, no runtime-load WARNINGS specifically.
    runtime_load_warnings = [
        w for w in validator.WARNINGS if "runtime-load check" in w
    ]
    assert validator.ERRORS == [], validator.ERRORS
    assert runtime_load_warnings == [], runtime_load_warnings


@_skip_no_runtime
def test_valid_baseadapter_subclass_passes(validator, tmp_path, monkeypatch):
    """The happy path: adapter.py defines a class inheriting from
    BaseAdapter. All 8 production templates match this shape."""
    adapter = (
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "\n"
        "class MyAdapter(BaseAdapter):\n"
        "    @staticmethod\n"
        "    def name():\n"
        "        return 'test-adapter'\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert validator.ERRORS == [], validator.ERRORS


@_skip_no_runtime
def test_adapter_with_no_baseadapter_subclass_errors(validator, tmp_path, monkeypatch):
    """The most insidious silent-failure mode: adapter.py imports
    cleanly, defines classes, but NONE inherit from BaseAdapter. The
    runtime's class-discovery would silently skip this file and fall
    through to the default executor — workspace would 'work' but with
    the wrong runtime. Must hard-error."""
    adapter = (
        "class JustSomePlainClass:\n"
        "    def run(self): pass\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "no class inheriting from" in e and "BaseAdapter" in e
        for e in validator.ERRORS
    ), validator.ERRORS


@_skip_no_runtime
def test_adapter_with_syntax_error_errors(validator, tmp_path, monkeypatch):
    """SyntaxError at import is the same failure mode that crashes
    workspace boot. Catch it here."""
    adapter = "this is not valid python at all\n"
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any("failed to import" in e for e in validator.ERRORS), validator.ERRORS


@_skip_no_runtime
def test_adapter_with_import_error_errors(validator, tmp_path, monkeypatch):
    """ImportError during adapter.py exec — same failure mode as
    workspace boot. The error message should point the contributor at
    requirements.txt as the right fix."""
    adapter = (
        "import this_package_definitely_does_not_exist_0xdeadbeef\n"
        "from molecule_runtime.adapters.base import BaseAdapter\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)
    validator.check_adapter_runtime_load()
    assert any(
        "failed to import" in e and "ModuleNotFoundError" in e
        for e in validator.ERRORS
    ), validator.ERRORS


def test_runtime_not_installed_warns_not_errors(validator, tmp_path, monkeypatch):
    """If the validator runs in an env without molecule-ai-workspace-runtime,
    we WARN (loud) but don't error — hard-erroring would say 'your adapter
    is broken' when the actual issue is the CI infra. Mock the import to
    simulate this regardless of what's installed locally."""
    adapter = (
        "from molecule_runtime.adapters.base import BaseAdapter\n"
        "class A(BaseAdapter): pass\n"
    )
    _materialise(tmp_path, adapter_py=adapter)
    monkeypatch.chdir(tmp_path)

    # Force the runtime import to fail by hiding the module.
    import sys
    saved = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k.startswith("molecule_runtime")}
    saved_meta = sys.meta_path[:]
    class _Block:
        def find_spec(self, name, path=None, target=None):
            if name == "molecule_runtime" or name.startswith("molecule_runtime."):
                raise ImportError(f"blocked for test: {name}")
            return None
    sys.meta_path.insert(0, _Block())
    try:
        validator.check_adapter_runtime_load()
    finally:
        sys.meta_path[:] = saved_meta
        sys.modules.update(saved)

    assert validator.ERRORS == [], validator.ERRORS
    assert any(
        "skipping runtime-load check" in w
        for w in validator.WARNINGS
    ), validator.WARNINGS
