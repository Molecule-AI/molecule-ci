# molecule-ci

Shared CI workflows for the Molecule AI ecosystem. Every plugin, workspace template, and org template repo calls these reusable workflows to enforce a standard validation gate.

## Usage

### Plugin repos (`molecule-ai-plugin-*`)

Skill/prompt-shaped plugins (the majority — Python with `plugin.yaml` + `SKILL.md`/`hooks`/`skills`/`rules`):

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-plugin.yml@main
```

Go-binary plugins (compiled `provisionhook.Registry` registrants — `molecule-ai-plugin-gh-identity`, `molecule-ai-plugin-github-app-auth`):

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  secret-scan:
    uses: Molecule-AI/molecule-core/.github/workflows/secret-scan.yml@staging
  validate-go:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-go-plugin.yml@staging
    # with:
    #   packages: ./internal/...   # narrower test scope when cmd/ requires external modules
```

### Workspace template repos (`molecule-ai-workspace-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-workspace-template.yml@main
```

### Org template repos (`molecule-ai-org-template-*`)

```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  validate:
    uses: Molecule-AI/molecule-ci/.github/workflows/validate-org-template.yml@main
```

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

### validate-go-plugin

| Check | Severity | What it catches |
|---|---|---|
| `go mod tidy` clean | Error | Drifting go.mod/go.sum |
| `go build` | Error | Broken compile |
| `go vet` | Error | Suspicious constructs |
| `go test -race -count=1` | Error | Concurrency bugs + cache lies |
| `gofmt -l` | Error | Unformatted files |
| `govulncheck` | Error | Reachable CVEs in deps |

Inputs:
- `go-version` (default `"1.25"`)
- `packages` (default `"./..."`) — pin narrower for plugins whose top-level cmd packages require external modules

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

## License

Business Source License 1.1 — © Molecule AI.
