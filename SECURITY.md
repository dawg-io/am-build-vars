# Security policy

## Reporting a vulnerability

Report privately through GitHub Security Advisories:
**[Report a vulnerability](https://github.com/dawg-io/am-build-vars/security/advisories/new)**.

Please do not open a public issue for anything with security impact. A public
issue is a disclosure, and this action runs inside other people's CI.

Include what you have: the version or commit, a workflow that reproduces it,
and what an attacker gets out of it. A proof of concept helps but is not
required to file.

There is no bounty and no guaranteed response time. Reports are read and
answered on the advisory thread; if one goes quiet, please ping it rather than
assuming it was declined.

## Supported versions

Fixes land on `main` and ship in the newest `v1.x` release. The `v1` tag moves
to that release, so a workflow pinned to `@v1` picks a fix up on its next run.
Older `v1.x` tags are not backported.

## Scope

In scope — anything that lets a value cross a boundary this action says it
maintains:

- A committed `am-build-vars.yml`, a `defaults` input, or a shared store
  causing a key that `validate_key` should reject to reach `$GITHUB_ENV` —
  `PATH`, `GITHUB_TOKEN`, or any other runner-owned name.
- A store artifact written by a fork's run being read by a later run of the
  base repository. This is the `pull_request_target` / `workflow_run`
  escalation shape and the eligibility rules in
  [`scripts/store.py`](scripts/store.py) exist to stop it.
- Two different `share-scope` values resolving to the same store artifact, so a
  value published on one branch surfaces on another.
- `share-token` reaching any host other than the GitHub API — in particular
  following the artifact download's redirect to signed storage with the token
  attached.
- Any path that makes this action execute a value out of a config file or a
  store rather than treating it as data.

Out of scope — documented behaviour, not vulnerabilities:

- **Values in job logs.** Every key exported to `$GITHUB_ENV` is echoed with
  its value in the `env:` block the runner writes for each later step. That is
  GitHub's behaviour for any `$GITHUB_ENV` write. Use `export-env: 'false'` and
  the `json` output to keep a value out of the logs.
- **Values are not masked.** `am-build-vars.yml` is committed in plaintext, so
  masking would be false comfort, and it would corrupt logs for ordinary values
  like `20` or `true`.
- **The shared store is an artifact, not a secret store.** In a public
  repository artifacts are downloadable by anyone. Never publish anything you
  would not commit.
- **A store written by the current run is trusted**, which is what lets a
  fork's pull request share values between its own jobs. Anything that can run
  in your job can therefore write the store that job later reads — including a
  `pull_request_target` workflow that checks out and executes fork code, which
  is an anti-pattern for other reasons too.
- **Concurrent producers race.** Two runs publishing to one scope both read,
  then both write; the later write wins. Use a `concurrency:` group.
- Secrets a repository chose to commit to `am-build-vars.yml`. The README, the
  example file and this document all say not to.

See the [Security section of the README](README.md#security) for the full
reasoning behind each of these.
