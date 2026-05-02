# molecule-ci

Shared CI workflows for the Molecule AI ecosystem. Every plugin, workspace template, and org template repo calls these reusable workflows to enforce a standard validation gate.

## Usage

### Plugin repos (`molecule-ai-plugin-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-plugin.yml@v1
```

### Workspace template repos (`molecule-ai-workspace-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-workspace-template.yml@v1
```

### Org template repos (`molecule-ai-org-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-org-template.yml@v1
```

### Workspace template repos publishing to GHCR

```yaml
# .github/workflows/publish-image.yml
name: publish-image
on:
  push:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
  packages: write
jobs:
  publish:
    uses: Molecule-AI/molecule-ci/.github/workflows/publish-template-image.yml@v1
    secrets: inherit
```

Also fires from the runtime-publish cascade (`repository_dispatch`) so a fresh `molecule-ai-workspace-runtime` PyPI release auto-rebuilds every template image.

### Any repo with auto-merge enabled

PR-time guards (currently: disable auto-merge on follow-up push). Consume from a thin caller:

```yaml
# .github/workflows/pr-guards.yml
name: pr-guards
on:
  pull_request:
    types: [synchronize]
permissions:
  pull-requests: write
jobs:
  disable-auto-merge-on-push:
    uses: Molecule-AI/molecule-ci/.github/workflows/disable-auto-merge-on-push.yml@v1
```

When the team lands more PR-time guards in this repo, add them as additional jobs in the same caller — keeps each consuming repo's footprint to one file.

## What each workflow validates

### validate-plugin

| Check | Severity | What it catches |
|---|---|---|
| `plugin.yaml` exists | Error | Missing manifest |
| Required fields (name, version, description) | Error | Incomplete plugin |
| Has content (SKILL.md, hooks/, skills/, or rules/) | Error | Empty plugin |
| SKILL.md starts with heading | Warning | Bad formatting |
| No committed secrets | Error | Leaked API keys |
| No build artifacts | Error | node_modules, __pycache__ |

### validate-workspace-template

| Check | Severity | What it catches |
|---|---|---|
| `config.yaml` exists | Error | Missing config |
| Required fields (name, runtime) | Error | Incomplete template |
| `template_schema_version: 1` | Error | Missing version contract |
| Known runtime check | Warning | Typo in runtime name |
| `adapter.py` imports molecule_runtime | Warning | Legacy imports |
| Dockerfile builds | Error | Broken image |
| molecule-ai-workspace-runtime dependency | Warning | Missing base package |
| No committed secrets | Error | Leaked API keys |

### validate-org-template

| Check | Severity | What it catches |
|---|---|---|
| `org.yaml` exists | Error | Missing org definition |
| Required fields (name) | Error | Incomplete template |
| Workspace structure valid | Error | Malformed hierarchy |
| `files_dir` references exist | Warning | Broken system-prompt paths |
| `template_schema_version` present | Warning | Missing version contract |
| No committed secrets | Error | Leaked API keys |

### disable-auto-merge-on-push

PR-time safety guard. When `pull_request:synchronize` fires (= a new commit pushed to an open PR) and auto-merge is already enabled, this workflow disables auto-merge and posts a comment requiring the operator to re-engage explicitly.

**Why it exists:** on 2026-04-27, molecule-core PR #2174 auto-merged with only its first commit because the second commit was pushed AFTER the merge queue had locked the PR's SHA. The second commit ended up orphaned on a merged-and-deleted branch.

**Pairs with the org-wide repo setting** "Automatically delete head branches" (already enabled on all 10 Molecule-AI repos). Defense in depth:

1. Repo setting blocks pushes to a merged-and-deleted branch (catches the post-merge orphan case).
2. This workflow catches the in-queue race (push during queue processing) by force-disabling auto-merge.

Together they cover the full lifecycle of "auto-merge enabled → new commits arrive" without operator discipline.

**False-positive note:** if a CI bot pushes (dependency update, secret rotation), this also disables auto-merge. That's intentional — the operator who originally enabled auto-merge gets notified and re-engages, which is exactly the verify-after-machine-edits behavior we want.

## publish-template-image

Builds + publishes Docker template images for workspace runtimes to GHCR (`ghcr.io/molecule-ai/workspace-template-<runtime>:latest` plus a per-commit `:sha-<7>` tag). Auto-derives `<runtime>` from the caller repo name (`molecule-ai-workspace-template-<runtime>`).

**Triggers** (caller-side `on:` block):

| Event | When | Source |
|---|---|---|
| `push` to `main` | Template Dockerfile / config / adapter changes | Caller commit |
| `workflow_dispatch` | Manual rebuild | Operator |
| `repository_dispatch` (cascade) | New `molecule-ai-workspace-runtime` PyPI release | molecule-core `publish-runtime.yml` fans out to every template repo |

**Inputs** (all optional):

| Input | Default | Purpose |
|---|---|---|
| `runtime_name` | derived from repo name | Override only when image should diverge from `molecule-ai-workspace-template-<runtime>` convention |
| `runtime_version` | empty (Dockerfile pin wins) | Forwarded as `RUNTIME_VERSION` build-arg → unique cache key per version. Cascade builds set this to the just-published wheel version so each rebuild gets a fresh `pip install`. |

**Secrets:** `secrets: inherit` (uses caller's `GITHUB_TOKEN` for GHCR push — no custom secrets needed).

**Outputs:** `image` (full ref pushed) and `sha` (short tag).

**Pipeline order** (each step is a publish gate — fail = no GHCR push):

1. **Lint** — bare imports of runtime modules (e.g. `from plugins import ...` instead of `from molecule_runtime.plugins import ...`). Module list pulled live from the latest wheel's `_runtime_modules.json` so the lint never drifts from the rewriter. Catches the 2026-04-27 5-template ImportError outage class.
2. **Static import smoke** — boots the image and `import`s every `/app/*.py`, exercising adapter-level module-load failures and runtime version skew.
3. **Boot smoke** (`MOLECULE_SMOKE_MODE=1`) — actually runs `executor.execute()` against stub deps + stub creds, catching **lazy imports** buried inside `async def execute(...)` bodies that the static smoke can't see (the a2a-sdk v0→v1 migration shipped 5 such regressions). Also consults `runtime_wedge.is_wedged()` to upgrade provisional PASS to FAIL when an adapter marked the runtime wedged.
4. **Push to GHCR** — only after all three gates pass.

**Smoke timeout calibration** (load-bearing — do not lower without re-testing with an injected wedge):

- `MOLECULE_SMOKE_TIMEOUT_SECS=90` — inner timeout. Outlasts claude-agent-sdk's 60s `initialize()` handshake so the adapter's wedge-catch arm runs **before** smoke gives up. Lowering this back to the original 10s blinds the gate to PR-25-class init-wedge bugs.
- `timeout 120` outer wrapper — runner-level safety net; surfaces `exit 124` (smoke_mode itself wedged) as a distinct error from `exit 1` (adapter ImportError / wedge).
- 90s/120s pair landed in [PR #33](https://github.com/Molecule-AI/molecule-ci/pull/33) (2026-05-02) for SDK-init-wedge coverage. The workflow's inline comment is the source of truth — read it before changing.

**Cross-references:**

- Boot-smoke contract + wedge protocol: `molecule-core/workspace/smoke_mode.py` docstring
- Cascade trigger (runtime publish → template rebuild fan-out): `molecule-core/.github/workflows/publish-runtime.yml`
- Why this gate exists at all (original outage post-mortem): the 2026-04-27 `RuntimeCapabilities` ImportError that shipped to `:latest` because the old smoke only inspected the entrypoint string

## License

Business Source License 1.1 — © Molecule AI.
