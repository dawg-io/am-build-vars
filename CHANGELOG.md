# Changelog

Notable changes to am-build-vars. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`v1` is a moving tag: it points at the newest `v1.x` release, so a workflow
pinned to `@v1` picks up each release below as soon as it ships. Pin a full
version, or a commit SHA, to opt out of that.

## [1.0.0] - 2026-09-03

First release. Everything below is the surface `v1` starts from.

### Added

- Read per-repository build variables from a committed `am-build-vars.yml`,
  found in the repository root or in `.github/`, and export every key to
  `$GITHUB_ENV` under its own name. The key in the file is the variable name —
  same name, same case, no prefix.
- `defaults` input: fleet-wide defaults as a YAML mapping, applied to any key
  the config file does not define. This is what lets one byte-identical managed
  workflow produce different results per repository.
- Outputs `json`, `keys`, `config-file-used` for the cases the environment
  export cannot cover — the same step, another job, and `runs-on` /
  `strategy.matrix`.
- A shared store, so a value a run *computes* can be read back by a later step
  in another job or another workflow file: `share` and `share-env` publish,
  `load-shared` reads, `share-scope` namespaces, and `sources`, `shared-json`
  and `shared-run-id` say where each value came from. The store is an Actions
  artifact holding one JSON file; publishing merges into it rather than
  replacing it.
- `config-file`, `export-env`, `fail-on-missing`, `share-token` and
  `share-retention-days` inputs.
- Key names are validated against `[A-Za-z_][A-Za-z0-9_]*` and rejected when
  they collide with a runner-owned variable, so a config file or a store cannot
  rewrite `PATH` or `GITHUB_TOKEN` for the rest of the job.
- A store artifact written by a fork's run is never read by a later run of the
  base repository, which closes the `pull_request_target` / `workflow_run`
  escalation shape. A store from the current run is still trusted, so a fork's
  pull request can share between its own jobs.
- Runner prerequisites (`python3`, PyYAML) are checked up front and reported by
  name rather than as a stack trace.

### Known limits

- Top-level keys only: no deep merge, no dotted-path access.
- No locking around the shared store — concurrent producers race, last write
  wins.
- A store lives exactly as long as its artifact.
- Windows runners are not supported.
