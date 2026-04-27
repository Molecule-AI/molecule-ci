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
REQUIRED_KEYS = ["name", "runtime", "template_schema_version"]
OPTIONAL_KEYS = [
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
    for key in REQUIRED_KEYS:
        if key not in config:
            err(f"config.yaml: missing required key `{key}`")
    runtime = config.get("runtime")
    if runtime and runtime not in KNOWN_RUNTIMES:
        warn(
            f"config.yaml: runtime `{runtime}` not in known set "
            f"{sorted(KNOWN_RUNTIMES)} — OK for custom runtimes; "
            f"if canonical, add it to KNOWN_RUNTIMES in validate-workspace-template.py"
        )
    sv = config.get("template_schema_version")
    if sv is not None and not isinstance(sv, int):
        err(
            f"config.yaml: template_schema_version must be int, "
            f"got {type(sv).__name__}={sv!r}"
        )

    unknown = set(config.keys()) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
    if unknown:
        warn(
            f"config.yaml: unknown top-level keys {sorted(unknown)} — "
            f"may be drift. If intentional, add them to OPTIONAL_KEYS."
        )


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


def main() -> None:
    check_dockerfile()
    check_config_yaml()
    check_requirements()
    check_adapter()

    for w in WARNINGS:
        print(f"::warning::{w}")
    for e in ERRORS:
        print(f"::error::{e}")
    if ERRORS:
        sys.exit(1)
    print(f"✓ Template validation passed ({len(WARNINGS)} warning(s))")


if __name__ == "__main__":
    main()
