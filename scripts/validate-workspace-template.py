#!/usr/bin/env python3
"""Prototype of the beefed-up validate-workspace-template.py.

Run from a template repo's root. Surfaces hard structural drift in
Dockerfile + config.yaml + requirements.txt against the canonical
contract. Replaces the existing soft-warnings-only validator at
molecule-ci/scripts/validate-workspace-template.py.
"""
import os, re, sys
import yaml

ERRORS: list[str] = []
WARNINGS: list[str] = []

def err(msg: str) -> None:
    ERRORS.append(msg)

def warn(msg: str) -> None:
    WARNINGS.append(msg)


# ───────────────────────────────────────────────────────────── Dockerfile

def check_dockerfile() -> None:
    if not os.path.isfile("Dockerfile"):
        warn("no Dockerfile — skipping container drift checks (library-only template?)")
        return
    df = open("Dockerfile").read()

    if not re.search(r"^FROM python:3\.11-slim\b", df, re.MULTILINE):
        err("Dockerfile: must base on `FROM python:3.11-slim` — see contract doc")

    if not re.search(r"^ARG RUNTIME_VERSION", df, re.MULTILINE):
        err(
            "Dockerfile: missing `ARG RUNTIME_VERSION=`. "
            "This arg invalidates the pip-install cache when the cascade "
            "publishes a new wheel; without it, the cascade silently ships "
            "the previous runtime (cache trap observed 2026-04-27, 5x in a row)."
        )

    if "molecule-ai-workspace-runtime" not in df and not (
        os.path.isfile("requirements.txt")
        and "molecule-ai-workspace-runtime" in open("requirements.txt").read()
    ):
        err("Dockerfile + requirements.txt: must install `molecule-ai-workspace-runtime`")

    if "${RUNTIME_VERSION}" not in df and "$RUNTIME_VERSION" not in df:
        err(
            "Dockerfile: must reference `${RUNTIME_VERSION}` in a pip install RUN block. "
            'Pattern: `if [ -n "${RUNTIME_VERSION}" ]; then '
            'pip install --no-cache-dir --upgrade "molecule-ai-workspace-runtime==${RUNTIME_VERSION}"; fi`'
        )

    if not re.search(r"useradd[^\n]*\bagent\b", df):
        err(
            "Dockerfile: must create the `agent` user "
            "(`RUN useradd -u 1000 -m -s /bin/bash agent`). "
            "Runtime drops to uid 1000; without it, claude-code refuses "
            "`--dangerously-skip-permissions` for safety."
        )

    has_direct_entrypoint = bool(
        re.search(r'(ENTRYPOINT|CMD)\s*\[?\s*"?molecule-runtime"?', df)
    )
    has_custom_entrypoint = bool(
        re.search(r'ENTRYPOINT\s*\[?\s*"?(/?[\w./-]*entrypoint\.sh|/?[\w./-]*start\.sh)', df)
    )
    if not has_direct_entrypoint and not has_custom_entrypoint:
        err(
            "Dockerfile: must end at `molecule-runtime` "
            "(`ENTRYPOINT [\"molecule-runtime\"]` or via custom "
            "entrypoint.sh / start.sh that exec's molecule-runtime)"
        )
    if has_custom_entrypoint:
        m = re.search(r'ENTRYPOINT\s*\[?\s*"?(/?[\w./-]+)', df)
        if m:
            ep_in_image = m.group(1).lstrip("/")
            ep_local = os.path.basename(ep_in_image)
            if os.path.isfile(ep_local):
                if "molecule-runtime" not in open(ep_local).read():
                    err(
                        f"Dockerfile uses ENTRYPOINT [{ep_in_image}] but "
                        f"{ep_local} does not exec `molecule-runtime`"
                    )
            else:
                warn(
                    f"Dockerfile points ENTRYPOINT at {ep_in_image} but "
                    f"{ep_local} not found in repo root — verify it's COPYed in"
                )


# ───────────────────────────────────────────────────────────── config.yaml

KNOWN_RUNTIMES = {
    "langgraph",
    "claude-code",
    "crewai",
    "autogen",
    "deepagents",
    "hermes",
    "gemini-cli",
    "openclaw",
}

# ──────────────────────────────────────────── schema versioning
#
# `template_schema_version: int` in each template's config.yaml selects
# which contract this validator enforces. Versions are FROZEN once
# shipped — never edit a SCHEMA_V* constant in place. To bump:
#
#   1. Add `SCHEMA_V<N+1>_REQUIRED_KEYS` / `SCHEMA_V<N+1>_OPTIONAL_KEYS`
#      describing the new contract.
#   2. Add `_check_schema_v<N+1>(config)` that enforces it.
#   3. Add the entry to SCHEMA_CHECKS below.
#   4. Move version N from KNOWN_SCHEMA_VERSIONS to
#      DEPRECATED_SCHEMA_VERSIONS so existing v<N> templates warn but
#      still pass — buys a deprecation window.
#   5. Ship a corresponding migration in scripts/migrate-template.py's
#      MIGRATIONS table (key = N, value = callable that produces the
#      v<N+1> dict from a v<N> dict).
#   6. Run migrate-template.py on each consumer template repo as a PR.
#   7. After all consumers migrate, drop version N from
#      DEPRECATED_SCHEMA_VERSIONS in a follow-up PR.
#
# This discipline means a schema version always has exactly one valid
# enforcement function, never "branch on minor variants" — the whole
# point of versioning is to avoid that drift.

KNOWN_SCHEMA_VERSIONS: set[int] = {1}
DEPRECATED_SCHEMA_VERSIONS: set[int] = set()

SCHEMA_V1_REQUIRED_KEYS = ["name", "runtime", "template_schema_version"]
SCHEMA_V1_OPTIONAL_KEYS = [
    "description",
    "version",
    "tier",
    "model",
    "models",
    "runtime_config",
    "env",
    "skills",
    "tools",
    "a2a",
    "delegation",
    "prompt_files",
    "bridge",
    "governance",
]


def _check_schema_v1(config: dict) -> None:
    """v1 contract — the keys frozen as of monorepo task #90's Phase 2.
    Currently every production template runs this version. Do NOT edit
    in place; add v2 instead and migrate consumers (see header)."""
    for key in SCHEMA_V1_REQUIRED_KEYS:
        if key not in config:
            err(f"config.yaml: missing required key `{key}`")
    runtime = config.get("runtime")
    if runtime and runtime not in KNOWN_RUNTIMES:
        warn(
            f"config.yaml: runtime `{runtime}` not in known set "
            f"{sorted(KNOWN_RUNTIMES)} — OK for custom runtimes; "
            f"if canonical, add it to KNOWN_RUNTIMES in validate-workspace-template.py"
        )
    unknown = set(config.keys()) - set(SCHEMA_V1_REQUIRED_KEYS) - set(SCHEMA_V1_OPTIONAL_KEYS)
    if unknown:
        warn(
            f"config.yaml: unknown top-level keys {sorted(unknown)} — "
            f"may be drift. If intentional, add them to SCHEMA_V1_OPTIONAL_KEYS."
        )


SCHEMA_CHECKS = {
    1: _check_schema_v1,
}


def check_config_yaml() -> None:
    if not os.path.isfile("config.yaml"):
        err("config.yaml: missing at repo root")
        return
    with open("config.yaml") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            err(f"config.yaml: invalid YAML — {e}")
            return
    if not isinstance(config, dict):
        err(f"config.yaml: root must be a mapping, got {type(config).__name__}")
        return

    # Schema-version dispatch. Validate the version field shape first
    # so error messages are actionable.
    sv = config.get("template_schema_version")
    if sv is None:
        err("config.yaml: missing required key `template_schema_version`")
        # Can't dispatch without a version. Don't fall through to v1
        # checks — that would mask the missing-version error.
        return
    if not isinstance(sv, int):
        err(
            f"config.yaml: template_schema_version must be int, "
            f"got {type(sv).__name__}={sv!r}"
        )
        return

    if sv in DEPRECATED_SCHEMA_VERSIONS:
        latest = max(KNOWN_SCHEMA_VERSIONS)
        warn(
            f"config.yaml: template_schema_version={sv} is deprecated; "
            f"migrate to v{latest} via "
            f"`python3 scripts/migrate-template.py --to {latest} .`. "
            f"Support for v{sv} will be removed in a future cycle."
        )
    elif sv not in KNOWN_SCHEMA_VERSIONS:
        valid = sorted(KNOWN_SCHEMA_VERSIONS | DEPRECATED_SCHEMA_VERSIONS)
        err(
            f"config.yaml: template_schema_version={sv} is unknown — "
            f"this validator understands {valid}. Either bump the "
            f"validator (add a SCHEMA_V{sv} block) or correct the version."
        )
        return

    SCHEMA_CHECKS[sv](config)


# ───────────────────────────────────────────────────────────── requirements.txt

def check_requirements() -> None:
    if not os.path.isfile("requirements.txt"):
        warn("no requirements.txt — Dockerfile must install runtime by other means")
        return
    reqs = open("requirements.txt").read()
    if "molecule-ai-workspace-runtime" not in reqs:
        err("requirements.txt: must declare `molecule-ai-workspace-runtime` as a dependency")


# ───────────────────────────────────────────────────────────── adapter.py

def check_adapter() -> None:
    """Static-text adapter checks. Fast — no imports."""
    if not os.path.isfile("adapter.py"):
        warn("no adapter.py — runtime will use the default langgraph executor from the wheel")
        return
    content = open("adapter.py").read()
    # The original validator's warning ("don't import molecule_runtime") was
    # backwards — that's the canonical package name. The previous check shipped
    # for ~2 weeks producing false-positive warnings. Removed.
    if re.search(r"\bfrom molecule_ai\b|\bimport molecule_ai\b", content):
        warn(
            "adapter.py imports `molecule_ai` — that's a pre-#87 package name; "
            "use `molecule_runtime`"
        )


def check_adapter_runtime_load() -> None:
    """Strong adapter contract: import adapter.py the same way the runtime
    does at workspace boot, and assert at least one class in it inherits
    from molecule_runtime.adapters.base.BaseAdapter.

    The Docker build smoke test in validate-workspace-template.yml builds
    the image but doesn't RUN it — adapter.py is only imported at
    container startup. So a template with a syntactically-valid Dockerfile
    + a broken adapter.py (wrong base class, ImportError on a missing
    framework dep, typo) builds clean and fails on first user prompt.
    This check exercises the same class-resolution path the runtime uses,
    so a passing validator means a passing workspace boot for the
    adapter-load step.

    Skip conditions:
      - No adapter.py exists. Templates without one inherit the default
        langgraph executor from the wheel (intentional, not drift).
      - molecule-ai-workspace-runtime not importable in the validator
        environment. That's a CI-config bug — the workflow that runs
        this validator must `pip install molecule-ai-workspace-runtime`
        first. Warn loudly so the misconfiguration surfaces, but don't
        hard-fail (we'd be saying "your adapter is broken" when the
        actual cause is missing infra). The `pip install -r
        requirements.txt` step in validate-workspace-template.yml
        normally satisfies this transitively.

    Hard-error conditions:
      - adapter.py raises any exception during import. The same
        exception would crash workspace boot.
      - No class in the module inherits from BaseAdapter. The runtime's
        adapter-discovery would silently fall through to the default
        executor, ignoring this file — exactly the kind of human-error
        mode this contract is supposed to eliminate.
    """
    if not os.path.isfile("adapter.py"):
        return  # check_adapter() already warned; don't double-warn

    try:
        from molecule_runtime.adapters.base import BaseAdapter  # noqa: PLC0415
    except ImportError:
        warn(
            "adapter.py: skipping runtime-load check — "
            "`molecule-ai-workspace-runtime` not installed in the validator "
            "environment. The CI workflow that invokes this script must "
            "`pip install molecule-ai-workspace-runtime` (or `pip install "
            "-r requirements.txt`) first; otherwise this critical check is "
            "silently bypassed."
        )
        return

    # Load adapter.py as a module under a unique name so it doesn't
    # collide with any installed `adapter` package or with a previous
    # invocation in the same Python process.
    import importlib.util  # noqa: PLC0415
    import sys             # noqa: PLC0415

    module_name = "_template_adapter_under_validation"
    spec = importlib.util.spec_from_file_location(module_name, "adapter.py")
    if spec is None or spec.loader is None:
        err("adapter.py: cannot construct an import spec — file may be unreadable")
        return

    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # required so dataclass / pydantic refs resolve

    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        err(
            f"adapter.py: failed to import — `{type(e).__name__}: {e}`. "
            f"This is the same failure mode that crashes workspace boot at "
            f"runtime; the cure is to fix the adapter, not skip this check. "
            f"If the import fails because a transitive dep isn't installed in "
            f"this CI env, add it to the template's requirements.txt — that's "
            f"what the workspace container does, and the validator job "
            f"installs requirements.txt before running this check."
        )
        sys.modules.pop(module_name, None)
        return

    adapter_classes = [
        obj
        for name, obj in vars(mod).items()
        if isinstance(obj, type)
        and obj is not BaseAdapter
        and issubclass(obj, BaseAdapter)
    ]
    sys.modules.pop(module_name, None)

    if not adapter_classes:
        err(
            "adapter.py: no class inheriting from "
            "`molecule_runtime.adapters.base.BaseAdapter` found. "
            "The runtime resolves the adapter via class discovery — "
            "without a BaseAdapter subclass, workspace boot falls "
            "through to the default langgraph executor and ignores "
            "this file silently. If that's intentional, delete adapter.py."
        )


def main() -> None:
    check_dockerfile()
    check_config_yaml()
    check_requirements()
    check_adapter()
    check_adapter_runtime_load()

    for w in WARNINGS:
        print(f"::warning::{w}")
    for e in ERRORS:
        print(f"::error::{e}")
    if ERRORS:
        sys.exit(1)
    print(f"✓ Template validation passed ({len(WARNINGS)} warning(s))")


if __name__ == "__main__":
    main()
