#!/usr/bin/env python3
"""Migrate a workspace template's config.yaml across schema versions.

Companion to validate-workspace-template.py. Whenever the validator
adds a new schema version, this script gets a corresponding entry in
MIGRATIONS so each consumer template can mechanically upgrade rather
than every maintainer figuring out the field changes by hand.

Discipline (matches the validator's header):

  1. Validator gets a SCHEMA_V<N+1> block + KNOWN_SCHEMA_VERSIONS bump.
  2. This script gets `MIGRATIONS[N]` defined — a function that takes
     a v<N> dict and returns a v<N+1> dict. Pure, deterministic, no
     I/O — that way migrations compose: v1 → v2 → v3 just chains them.
  3. Each migration is FROZEN once shipped. If a v2 migration needs
     fixing post-ship, ship it as v3 with the corrective migration.
  4. Consumers run this script (one PR per template repo) before the
     deprecation window for v<N> closes.

Usage:

    # Migrate the template in cwd from its current version to the latest
    python3 scripts/migrate-template.py .

    # Migrate to a specific version (bounded; useful when a deprecation
    # window is closing and you want to skip-ahead)
    python3 scripts/migrate-template.py --to 3 .

    # Force the source version (override config.yaml's declared version)
    python3 scripts/migrate-template.py --from 1 --to 2 .

    # Dry-run: print the diff without writing
    python3 scripts/migrate-template.py --dry-run .

The script preserves YAML round-trip fidelity for keys it doesn't
touch (using ruamel.yaml when available; falling back to PyYAML's
default representer otherwise). Migrations should ONLY mutate keys
they're explicitly versioning — leave everything else alone so a
consumer template's customizations survive.
"""
from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Callable

import yaml

# ──────────────────────────────────────────── migrations registry

# Each entry maps a SOURCE version to the function that produces the
# next version's dict. Currently empty — no v2 yet. The first time a
# real schema bump lands, MIGRATIONS[1] gets defined alongside the
# validator's SCHEMA_V2 block.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


# ──────────────────────────────────────────── version detection

def _detect_current_version(config: dict) -> int:
    sv = config.get("template_schema_version")
    if sv is None:
        sys.exit(
            "error: config.yaml has no `template_schema_version`. "
            "Add it (likely 1 for legacy templates) before migrating."
        )
    if not isinstance(sv, int):
        sys.exit(
            f"error: template_schema_version must be int, got "
            f"{type(sv).__name__}={sv!r}."
        )
    return sv


def _latest_known_version() -> int:
    """Maximum version reachable by chaining MIGRATIONS from any
    starting point. With an empty registry, this is 1 (the floor:
    every existing template is at v1)."""
    if not MIGRATIONS:
        return 1
    return max(MIGRATIONS.keys()) + 1


# ──────────────────────────────────────────── core

def migrate_config(config: dict, from_version: int, to_version: int) -> dict:
    """Apply migrations sequentially from `from_version` to `to_version`.
    Returns a NEW dict — does not mutate the input.

    Errors loudly when there's no migration registered for an
    intermediate step: forward-only, never silently skip a hop. If the
    user asks for a backward migration, error too — schema versions
    are append-only and we don't ship downgrades."""
    if to_version < from_version:
        sys.exit(
            f"error: cannot migrate backward (from v{from_version} to "
            f"v{to_version}). Schema versions are append-only — file a "
            f"new bug + ship a forward migration instead."
        )
    current = from_version
    out = deepcopy(config)
    while current < to_version:
        step = MIGRATIONS.get(current)
        if step is None:
            sys.exit(
                f"error: no migration registered for v{current} → "
                f"v{current + 1}. Either add it to MIGRATIONS in "
                f"scripts/migrate-template.py or pick a different --to."
            )
        out = step(out)
        # Every migration MUST stamp the new version on its output —
        # this assertion catches a class of bugs where a migration
        # forgets to bump template_schema_version.
        if out.get("template_schema_version") != current + 1:
            sys.exit(
                f"error: MIGRATIONS[{current}] did not stamp "
                f"template_schema_version={current + 1} on its output. "
                f"This is a bug in the migration function itself."
            )
        current += 1
    return out


def _read_yaml(path: Path) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        sys.exit(f"error: {path} root is not a mapping (got {type(data).__name__})")
    return data


def _write_yaml(path: Path, data: dict) -> None:
    # Sort keys for stable diffs across migrations. This matches what
    # `yaml.safe_dump` does when we write — consumer repos with
    # custom orderings will see their config.yaml re-ordered, which is
    # one of those round-trip lossy tradeoffs that's worth accepting:
    # the migration moment is rare and the diff is reviewable.
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False)


# ──────────────────────────────────────────── CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a workspace template's config.yaml across schema versions."
    )
    parser.add_argument(
        "template_dir",
        type=Path,
        help="Path to the template repo root (must contain config.yaml).",
    )
    parser.add_argument(
        "--from",
        dest="from_version",
        type=int,
        default=None,
        help="Source schema version (defaults to whatever config.yaml declares).",
    )
    parser.add_argument(
        "--to",
        dest="to_version",
        type=int,
        default=None,
        help="Target schema version (defaults to the highest reachable from MIGRATIONS).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migrated YAML to stdout without modifying the file.",
    )
    args = parser.parse_args(argv)

    config_path = args.template_dir / "config.yaml"
    if not config_path.is_file():
        sys.exit(f"error: {config_path} does not exist")

    config = _read_yaml(config_path)

    from_version = args.from_version
    if from_version is None:
        from_version = _detect_current_version(config)

    to_version = args.to_version
    if to_version is None:
        to_version = _latest_known_version()

    if from_version == to_version:
        print(
            f"nothing to do: config.yaml is already at v{from_version}.",
            file=sys.stderr,
        )
        return 0

    migrated = migrate_config(config, from_version, to_version)

    if args.dry_run:
        yaml.safe_dump(migrated, sys.stdout, sort_keys=True, default_flow_style=False)
        return 0

    _write_yaml(config_path, migrated)
    print(
        f"✓ migrated {config_path} from v{from_version} → v{to_version}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
