<p align="center">
  <img src="docs/actionsmanager-logo.png" alt="ActionsManager" width="420">
</p>

# am-build-vars

A composite GitHub Action that reads per-repository build variables from a committed
`am-build-vars.yml` and hands them to the rest of the job — so one **byte-identical**
workflow file can produce different results in every repository it is deployed to.

```yaml
- uses: actions/checkout@v7
- uses: dawg-io/am-build-vars@v1
  with:
    defaults: |
      node_version: "20"

- run: echo "Building with Node $node_version"
```

## How it works

1. A repository commits an `am-build-vars.yml` — in the repository root, or in
   `.github/`. One filename, either home; the action finds it.
2. This action reads it, filling in any key the file does not define from the workflow's
   inline `defaults`.
3. **Every key becomes an environment variable with the same name**, for every later step
   in the job.

Step 3 is the whole interface. The key you write in the file *is* the variable name:

| In `am-build-vars.yml` | In a shell step | In an expression |
|---|---|---|
| `node_version: "20"` | `$node_version` | `${{ env.node_version }}` |
| `test_command: npm test` | `$test_command` | `${{ env.test_command }}` |
| `coverage_enabled: true` | `$coverage_enabled` | `${{ env.coverage_enabled }}` |

Same name, same case, no prefix. Nothing to declare up front, and the step needs no `id`.

Values computed *during* a run can join the same picture — see
[Sharing variables across workflows](#sharing-variables-across-workflows).

## The problem it solves

Tools that manage workflows across a fleet of repositories — [ActionsManager][am] among
them — apply the same workflow definition everywhere and use drift detection to keep it
that way. The moment a repository edits its copy to bump a Node version, it is flagged as
drifted.

But real teams need that variation. One service is on Node 20, the next is on Node 24,
a third needs a different runner label.

This action splits the two concerns apart:

| | Owned by | Identical across repos? |
|---|---|---|
| `.github/workflows/build.yml` | the fleet | **yes** — never edited per repo |
| `am-build-vars.yml` | the repository's own team | no — this is where variation lives |

The workflow carries fleet-wide defaults inline. A repository that needs something
different commits an `am-build-vars.yml` overriding only the keys that differ. Drift
detection stays happy because the workflow file never changes.

This action has no runtime dependency on ActionsManager and works standalone in any
repository. It makes no API calls at all unless you turn sharing on.

## Requirements

- **`actions/checkout` must run first.** This action reads a file from the workspace; it
  does not check out the repository itself.
- `python3` with PyYAML on the runner. What that costs you depends on the runner:

  | Runner | `python3` | PyYAML | You need to |
  |---|---|---|---|
  | `ubuntu-*` | preinstalled | preinstalled | nothing |
  | `macos-*` | preinstalled | **not present** | install PyYAML first |
  | self-hosted | varies | varies | install both |

  On macOS, and on any self-hosted runner missing it, add this before the action:

  ```yaml
  - uses: actions/setup-python@v7
    with:
      python-version: '3.x'
  - run: python3 -m pip install pyyaml
  ```

  The action checks both prerequisites up front and fails with an explicit message
  naming what is missing, rather than a stack trace.
- Windows runners are **not supported** in v1.

No Node modules and no build step. The implementation is three short Python files —
[`scripts/resolve.py`](scripts/resolve.py) merges the layers,
[`scripts/store.py`](scripts/store.py) looks the shared store up, and
[`scripts/common.py`](scripts/common.py) holds the handful of things both need. That small
surface is the point: it is a third-party action that touches your build configuration, so
it should be auditable in a coffee break.

Leave sharing off and this action makes no network calls of its own. Turn it on and it
lists the repository's artifacts through the REST API and uses
[`actions/upload-artifact`][upload] to write the store — see
[Sharing variables across workflows](#sharing-variables-across-workflows).

One honest caveat about that dependency: a step's `if:` decides whether the step *runs*,
not whether the runner *resolves* it. `actions/upload-artifact` is therefore fetched during
job setup on every run, sharing or not — you will see it in the "Prepare all required
actions" group. It only ever executes when there is something to publish.

## Usage

### Minimal

The repository's `am-build-vars.yml` is the only source of values.

```yaml
steps:
  - uses: actions/checkout@v7
  - uses: dawg-io/am-build-vars@v1

  - run: echo "Building with Node $node_version"
```

```yaml
# am-build-vars.yml
node_version: "20"
```

If the file does not exist, the action still succeeds — it just resolves zero keys.

### With inline defaults

This is the managed-workflow shape. `defaults` is a YAML mapping using exactly the same
syntax as the config file.

```yaml
steps:
  - uses: actions/checkout@v7
  - uses: dawg-io/am-build-vars@v1
    with:
      defaults: |
        node_version: "20"
        runner: ubuntu-latest
        coverage_enabled: true
        test_command: npm test

  - uses: actions/setup-node@v7
    with:
      node-version: ${{ env.node_version }}
  - run: ${{ env.test_command }}
  - run: npm run coverage
    if: env.coverage_enabled == 'true'
```

> **Why YAML for `defaults`?** It is the same syntax as the config file, parsed by the
> same code path, with the same type handling — one thing to learn instead of two, and a
> default can be moved into a repo's config file by copy-paste. JSON is valid YAML, so
> `defaults: '{"node_version": "20"}'` works too if you are generating the workflow.

### Per-repo override — the whole point

The workflow below is committed **identically** to every repository in the fleet:

```yaml
name: Build
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: dawg-io/am-build-vars@v1
        with:
          defaults: |
            node_version: "20"
            test_command: npm test

      - uses: actions/setup-node@v7
        with:
          node-version: ${{ env.node_version }}
      - run: npm ci
      - run: ${{ env.test_command }}
```

**Repo A** — no `am-build-vars.yml` at all:

```
node_version  = 20          (default)
test_command  = npm test    (default)
```

**Repo B** — `am-build-vars.yml` in the repository root:

```yaml
node_version: "24"
```

```
node_version  = 24          (from the file — overrides the default)
test_command  = npm test    (default — untouched)
```

Same workflow file, byte for byte. Different builds.

## Sharing variables across workflows

Everything above is static — known before the run starts. The other half of the problem is
a value the run **computes**: an image tag, a version, a build id, needed later by a
*different workflow file*. GitHub gives you `needs.<job>.outputs` inside one run and
nothing at all across runs, so this normally ends in hand-rolled `workflow_run` and
artifact plumbing in every repository — the per-repo drift this action exists to remove.

The shared store closes that gap. It is a small JSON file kept as an Actions artifact: one
step publishes into it, a later step anywhere in the repository reads it back as an
ordinary variable, same name-is-the-name rule.

**The producer** publishes what the run computed:

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@v7
      - run: echo "image_tag=sha-$(git rev-parse --short HEAD)" >> "$GITHUB_ENV"

      - uses: dawg-io/am-build-vars@v1
        with:
          share-env: image_tag
```

**The consumer** is a different workflow file, in a later run:

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read       # required to read the store
    steps:
      - uses: actions/checkout@v7
      - uses: dawg-io/am-build-vars@v1
        with:
          load-shared: true

      - run: ./deploy.sh "$image_tag"
```

No `needs:`, no job outputs, no artifact plumbing. The variable is simply there.

### Publishing: `share-env` or `share`

| Input | Takes | Use it for |
|---|---|---|
| `share-env` | **names** of environment variables | anything computed during the run |
| `share` | a YAML mapping, same syntax as `defaults` | literals and workflow expressions |

Prefer `share-env` for computed values. It reads the value straight out of the environment,
so a tag containing a quote, a brace or a newline never has to survive a trip through YAML:

```yaml
- uses: dawg-io/am-build-vars@v1
  with:
    # names, whitespace- or comma-separated — safe whatever the values hold
    share-env: image_tag deploy_target

    # fine for literals; this one is being pasted into a YAML document
    share: |
      release_channel: stable
```

A name that is not set when the step runs is an **error**, not an empty value — a typo in a
`share-env` list should not quietly publish `""`.

### Precedence

Publishing adds two layers to the two you already had. Lowest to highest:

| Layer | Comes from | Beats |
|---|---|---|
| `defaults` | the workflow's inline defaults | — |
| **shared** | the store, when `load-shared: true` | `defaults` |
| file | the committed `am-build-vars.yml` | shared |
| **share** | what this step is publishing | everything |

The committed file **outranks** the store deliberately: config a team just committed should
never lose to an artifact some run left behind months ago. In practice the two rarely meet —
a key like `image_tag` is one nobody commits.

What a step publishes outranks everything, so the value written to the store and the value
the rest of that job sees can never disagree.

The `sources` output says which layer each key actually came from — worth asserting on in a
test, and it prints no values:

```yaml
- uses: dawg-io/am-build-vars@v1
  id: vars
  with:
    load-shared: true

- run: echo '${{ steps.vars.outputs.sources }}'
  # {"image_tag":"shared","node_version":"file","runner":"default"}
```

### Scope

A scope is a namespace: steps using the same one see each other's values, different ones
are fully isolated. It defaults to the **current ref name**, so every branch reads and
writes its own store and a pull request cannot disturb `main`'s.

| Run | Scope | Store artifact |
|---|---|---|
| push to `main` | `main` | `am-build-vars-store-main` |
| push tag `v1.2.3` | `v1.2.3` | `am-build-vars-store-v1.2.3` |
| PR #123 | `123/merge` | `am-build-vars-store-123-merge-99b03923` |
| push to `feat/x` | `feat/x` | `am-build-vars-store-feat-x-79b4cc55` |

An artifact name cannot contain a `/`, so a scope carrying one is slugged. Slugging alone
would let `feat/x` and `feat-x` land on the *same* store — a value set on one branch
surfacing on the other — so a short digest of the original scope is appended whenever
slugging changed anything. Scopes that need no slugging keep the readable name, which is
the common case.

That default isolates, which also means a tag-triggered deploy will **not** see what a build
on `main` published. When you want it to, name the scope explicitly on both sides:

```yaml
- uses: dawg-io/am-build-vars@v1
  with:
    load-shared: true
    share-scope: main      # read what builds on main published
```

### The store accumulates

Publishing **merges** into the store rather than replacing it, so a build workflow sharing
`image_tag` and a deploy workflow sharing `deployed_at` both survive and a consumer sees
both. That merge is also why a producer needs `actions: read`: it reads the store it is
about to write back.

### Before you rely on it

- **Permissions.** Producing *and* consuming need `actions: read` in the job's
  `permissions:` block. Without it the step fails naming the missing permission, rather
  than quietly resolving nothing.
- **Artifacts expire.** The store lives exactly as long as its artifact — 90 days by
  default, or whatever `share-retention-days` and the repository's settings say. A value
  nobody republishes eventually disappears.
- **Every publishing run leaves its own copy**, and the reader takes the newest. That is
  where the history comes from, and also why a busy scope fills up a run list — set
  `share-retention-days` low for a store that only has to survive until the next job.
- **Concurrent producers race.** Two runs publishing to one scope at once both read, then
  both write; the later write wins and the earlier one's new keys are lost. Put a
  `concurrency:` group on the producing workflow if that matters.
- **A missing store is not an error.** The first run in a new scope resolves zero shared
  keys and carries on. Assert it yourself when a value is mandatory:

  ```yaml
  - if: env.image_tag == ''
    run: |
      echo "::error::nothing has published image_tag for this scope yet"
      exit 1
  ```
- **Never share a secret.** The store is an artifact, and in a public repository artifacts
  are downloadable by anyone. See [Security](#security).

## Inputs

| Input | Default | Description |
|---|---|---|
| `config-file` | `''` | Explicit path to the file, turning discovery off. Leave empty to search the root then `.github/`. |
| `defaults` | `''` | Fleet-wide defaults as a YAML mapping. Applied to any key the config file does not define. |
| `export-env` | `'true'` | Write every resolved key to `$GITHUB_ENV`, overwriting any existing variable of the same name. Set `'false'` to leave the job environment untouched. |
| `fail-on-missing` | `'false'` | Fail the step when the config file is absent, instead of falling back to defaults only. |
| `share` | `''` | Values to publish to the shared store, as a YAML mapping. |
| `share-env` | `''` | Names of environment variables to capture and publish, separated by whitespace or commas. |
| `load-shared` | `'false'` | Read the shared store for this scope and apply it. Needs `actions: read`. |
| `share-scope` | `github.ref_name` | The namespace shared values live in. Same scope, same store. |
| `share-token` | `github.token` | Token used to look the store artifact up. |
| `share-retention-days` | `''` | How long the store artifact is kept. Empty uses the repository default. |

## Outputs

| Output | Description |
|---|---|
| `json` | Every resolved key/value pair as a compact JSON object. |
| `keys` | The resolved key names as a sorted JSON array. |
| `config-file-used` | The path actually read, or `''` when only defaults were applied. |
| `sources` | Where each key came from, as JSON: `default`, `shared`, `file` or `share`. |
| `shared-json` | The values that came from the shared store, as JSON. `{}` when none did. |
| `shared-run-id` | The run whose store was applied, or `''`. Provenance for a value from outside this run. |

Every resolved key is also a step output under its own name, so a key called `json`, `keys`
or `sources` shadows the output of that name — the metadata wins, and the config value is
unreachable through `steps.<id>.outputs`. It is still in `env`. Only these three collide;
the hyphenated names cannot, because a key may not contain a hyphen.

## When environment variables are not enough

Three cases the env export cannot cover. All three are GitHub Actions constraints, not
choices this action makes.

| Case | Why | Use |
|---|---|---|
| The same step that runs the action | `$GITHUB_ENV` only takes effect in *later* steps | the `json` output |
| A different job | jobs do not share an environment | a config job (below), or [the shared store](#sharing-variables-across-workflows) |
| `runs-on`, `strategy.matrix` | evaluated before any step runs | a config job (below) |

For these, give the step an `id` and read the `json` output — every resolved key, as one
JSON object:

```yaml
- uses: dawg-io/am-build-vars@v1
  id: vars
- run: echo "Node ${{ fromJSON(steps.vars.outputs.json).node_version }}"
```

### Passing values to another job

Put the verbose form in the config job's `outputs:` block once. Everything downstream is
then a plain `needs.config.outputs.<key>`:

```yaml
jobs:
  config:
    runs-on: ubuntu-latest
    outputs:
      runner: ${{ fromJSON(steps.vars.outputs.json).runner }}
      test_matrix: ${{ fromJSON(steps.vars.outputs.json).test_matrix }}
    steps:
      - uses: actions/checkout@v7
      - uses: dawg-io/am-build-vars@v1
        id: vars
        with:
          defaults: |
            runner: ubuntu-latest
            test_matrix: ["20"]

  build:
    needs: config
    runs-on: ${{ needs.config.outputs.runner }}
    strategy:
      matrix:
        node: ${{ fromJSON(needs.config.outputs.test_matrix) }}
    steps:
      - uses: actions/setup-node@v7
        with:
          node-version: ${{ matrix.node }}
```

`test_matrix` is unwrapped twice: once by the config job's output expression, once by
`fromJSON` in `strategy.matrix`.

That pattern is the right one for *config* — values that exist before the run starts, which
`runs-on` and `strategy.matrix` need evaluated before any step runs. For a value the run
itself computes, reach for [the shared store](#sharing-variables-across-workflows) instead:
it needs no `needs:` edge at all, and it works across workflow files.

> Passing a value into a shell script? Prefer `env:` over inlining the expression — it
> keeps quoting sane and multi-line values intact:
>
> ```yaml
> - env:
>     NOTES: ${{ fromJSON(steps.vars.outputs.json).release_notes }}
>   run: printf '%s' "$NOTES" > notes.txt
> ```

## Structured values

**v1 resolves top-level keys only.** Precedence is evaluated per top-level key: a key
present in the config file replaces the default entirely. There is no deep merge, and
there is no dotted-path access.

Values, however, may be any YAML type:

| YAML value | Resolved to |
|---|---|
| `"20"` | `20` |
| `20` | `20` |
| `true` / `false` | `true` / `false` (lowercase strings) |
| `null` / empty | `` (empty string) |
| `[18, 20, 24]` | `[18,20,24]` (compact JSON) |
| `{target: prod}` | `{"target":"prod"}` (compact JSON) |
| a `\|` block scalar | the text, newlines intact |

Lists and mappings arrive as JSON strings, so consuming them takes a `fromJSON()` at the
point of use:

```yaml
# am-build-vars.yml
test_matrix: ["18", "20", "24"]
```

```yaml
strategy:
  matrix:
    node: ${{ fromJSON(needs.config.outputs.test_matrix) }}
```

> **Quote your version numbers.** YAML reads unquoted `20.10` as the float `20.1` and
> unquoted `on`, `yes` and `no` as booleans. `"20.10"` gives you the string you meant.

## Key naming

Keys must match `^[A-Za-z_][A-Za-z0-9_]*$` — letters, digits and underscores, not starting
with a digit. Use `node_version`, not `node-version` or `node.version`.

This is enforced rather than silently rewritten, so the JSON key and the environment
variable name are always the same string. A dashed key that became `node_version` in the
environment but stayed `node-version` in `json` would be a permanent source of confusion.

## Errors

The action fails, with a message naming the file, when:

- the YAML is malformed (the parser's line and column are included);
- the top level of the file or of `defaults` is not a mapping;
- `am-build-vars.yml` exists in **both** the root and `.github/` — the action refuses to
  pick one silently;
- an `am-build-vars.yaml` exists and no `.yml` does. Only `.yml` is read, and quietly
  building with fleet defaults instead of the config you wrote is the worse outcome, so
  this is an error telling you to rename it;
- a key is not a valid name, or collides with a runner-owned variable;
- `fail-on-missing: true` and the file is absent.

A missing config file is **not** an error in the default configuration.

Sharing adds its own, all of them naming what to fix:

- `share-env` names a variable that is not set when the step runs;
- a shared key is not a valid name, or collides with a runner-owned variable;
- the token cannot read the repository's artifacts — the message names the
  `actions: read` permission;
- the store is unreadable: not JSON, or written by a newer schema than this action reads.
  A corrupted store is an error rather than a silent fall-through to nothing, because the
  caller asked for values a previous run published and quietly building without them is
  the confusing outcome;
- the store would grow past 512 KiB. It is meant for build coordinates, not payloads.

Finding **no** store is not an error: the first run in a new scope resolves zero shared
keys and carries on.

## Security

Found something? Report it privately — [`SECURITY.md`](SECURITY.md) has the route
and says which of the behaviours below are deliberate rather than findings.

**`am-build-vars.yml` is committed to the repository.** In a public repo it is world
readable, and in a private one it is visible to everyone with read access and to every
fork and CI log consumer. **Never put secrets, tokens, or credentials in it.** Use GitHub
Actions secrets for those.

The action helps, but cannot save you from this:

- **This action never prints a value.** Its own log lines show key names and which layer
  each came from — enough to debug a resolution, not enough to leak one.
- **The runner does print them, though**, and that is the part worth understanding. Every
  key exported to `$GITHUB_ENV` is echoed *with its value* in the `env:` block the runner
  writes for each later step, so an exported value is visible to anyone who can read the
  job log. That is GitHub's behaviour for any `$GITHUB_ENV` write, not something this
  action can suppress. To keep a value out of the logs, set `export-env: 'false'` and
  consume it through the `json` output instead — nothing is exported, and nothing is
  echoed.
- Values are not masked. Masking would be false comfort for something already committed in
  plaintext, and would corrupt logs for ordinary values like `20` or `true`.
- Runner-owned environment variable names are rejected, so a config file cannot rewrite
  `PATH` or `GITHUB_TOKEN` for the rest of the job.
- The action reads no secrets and never writes to `am-build-vars.yml`. With sharing off
  it makes no network calls at all.

Sharing adds one more surface, so it has its own rules:

- **The shared store is an artifact, not a secret store.** In a public repository artifacts
  are downloadable by anyone; in a private one, by everyone with read access. Treat a
  published value as being exactly as public as the committed config file — never share a
  token, and never share anything you would not commit.
- **A store written by a fork is never read by a later run.** Each artifact records the run
  that produced it, and a store is only trusted when that run belongs to this repository —
  or *is* the current run, so a pull request from a fork can still share between its own
  jobs. This is what stops the `pull_request_target` and `workflow_run` shape, where a job
  holding a write token consumes something a fork wrote.
- **Scopes isolate by default.** The default scope is the current ref, so a pull request
  branch and `main` do not share a store unless you deliberately point them at one.
- **A store is not a trusted input.** Values loaded from one are held to the same key rules
  as everything else, so a hand-crafted store cannot rewrite `PATH` or `GITHUB_TOKEN` for
  the rest of the job. This action logs shared key names only — but a shared value is
  exported like any other, so it reaches the runner's own `env:` echo just the same. The
  bullet above applies to shared values too.
- **The store is only as trustworthy as the job that wrote it.** A store from the *current
  run* is always accepted, which is what lets a fork's pull request share between its own
  jobs. The flip side: anything that can run in your job can also write the store that job
  later reads. In a `pull_request_target` workflow that checks out and executes a fork's
  code — already an anti-pattern for other reasons — that is a route from fork-controlled
  code into a privileged job's environment.
- **The token stays with GitHub.** The artifact download redirects to signed storage on
  another host; that request is made with no credentials attached.

## Versioning

`@v1` is a **moving tag**. It points at the newest `v1.x` release, so a fix lands
in your workflow on its next run without you editing anything — and so does every
other change in that release. Breaking changes get a new major and a new tag; they
never arrive through `v1`.

```yaml
- uses: dawg-io/am-build-vars@v1        # newest v1.x — recommended
- uses: dawg-io/am-build-vars@v1.0.0    # exactly this release
- uses: dawg-io/am-build-vars@6f4c9d2   # exactly this commit
```

Pin the SHA if your policy is that a third-party action must never change under
you. That is the same trade every action makes: you stop getting fixes until you
bump it yourself. [`CHANGELOG.md`](CHANGELOG.md) is what `v1` moved through.

## Not in scope for v1

- Nested key resolution or deep merging.
- Reading GitHub repository variables or secrets.
- Writing or modifying `am-build-vars.yml`.
- Sharing between *different repositories* — a store is repository-local.
- Any locking around the shared store: concurrent producers race, last write wins.
- Outliving the store artifact's retention.
- Windows runners.

## Development

```bash
tests/local-test.sh    # runs the resolver against every fixture, no runner needed
```

`tests/local-test.sh` covers the whole merge offline, sharing included: the resolver never
touches the network, so a store is just a JSON file you can point it at.

[`.github/workflows/test.yml`](.github/workflows/test.yml) is the authoritative suite — it
runs the real composite action on a runner through every case above, including the
expected failures. Its `share-producer` / `share-consumer` pair is the end-to-end proof for
sharing: the consumer never mentions `needs.share-producer.outputs`, so the only route the
value can have taken is the store — the same route a second workflow file would take.

The macOS smoke job is skipped by default so pull requests do not pay for macOS minutes.
Label a PR **`ci:macos`** to run it — worth doing for any change to runner-facing
behaviour, since that job is what backs the macOS support claim above.

[`.github/dependabot.yml`](.github/dependabot.yml) keeps the actions referenced with
`uses:` up to date, weekly. Minor and patch bumps are grouped into one PR; majors get
their own. That covers the root `action.yml` too, so the `actions/upload-artifact` pin the
shared store writes through is kept current. There is no other ecosystem configured because
there is nothing else to track — the action ships no package dependencies, and PyYAML comes
from the runner image.

## License

MIT — see [LICENSE](LICENSE).

[am]: https://github.com/dawg-io
[upload]: https://github.com/actions/upload-artifact
