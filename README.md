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

This action has no runtime dependency on ActionsManager, makes no API calls, and works
standalone in any repository.

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

No Node modules, no build step, no network access. The entire implementation is
[`scripts/resolve.py`](scripts/resolve.py) — about 130 lines you can read in one sitting.
That small surface is the point: it is a third-party action that touches your build
configuration, so it should be auditable in a coffee break.

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

## Inputs

| Input | Default | Description |
|---|---|---|
| `config-file` | `''` | Explicit path to the file, turning discovery off. Leave empty to search the root then `.github/`. |
| `defaults` | `''` | Fleet-wide defaults as a YAML mapping. Applied to any key the config file does not define. |
| `export-env` | `'true'` | Write every resolved key to `$GITHUB_ENV`. Set `'false'` to leave the job environment untouched. |
| `fail-on-missing` | `'false'` | Fail the step when the config file is absent, instead of falling back to defaults only. |

## Outputs

| Output | Description |
|---|---|
| `json` | Every resolved key/value pair as a compact JSON object. |
| `keys` | The resolved key names as a sorted JSON array. |
| `config-file-used` | The path actually read, or `''` when only defaults were applied. |

## When environment variables are not enough

Three cases the env export cannot cover. All three are GitHub Actions constraints, not
choices this action makes.

| Case | Why | Use |
|---|---|---|
| The same step that runs the action | `$GITHUB_ENV` only takes effect in *later* steps | the `json` output |
| A different job | jobs do not share an environment | a config job (below) |
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

## Security

**`am-build-vars.yml` is committed to the repository.** In a public repo it is world
readable, and in a private one it is visible to everyone with read access and to every
fork and CI log consumer. **Never put secrets, tokens, or credentials in it.** Use GitHub
Actions secrets for those.

The action helps, but cannot save you from this:

- Resolved values are **never printed**. The log shows key names and whether each came
  from the file or from a default — enough to debug a resolution, not enough to leak one.
  What downstream steps do with the values is up to them.
- Values are not masked. Masking would be false comfort for something already committed in
  plaintext, and would corrupt logs for ordinary values like `20` or `true`.
- Runner-owned environment variable names are rejected, so a config file cannot rewrite
  `PATH` or `GITHUB_TOKEN` for the rest of the job.
- The action makes no network calls, reads no secrets, and never writes to
  `am-build-vars.yml`.

## Not in scope for v1

- Cross-job variable sharing (use the `needs:` pattern above).
- Nested key resolution or deep merging.
- Reading GitHub repository variables, secrets, or anything from the API.
- Writing or modifying `am-build-vars.yml`.
- Windows runners.

## Development

```bash
tests/local-test.sh    # runs the resolver against every fixture, no runner needed
```

[`.github/workflows/test.yml`](.github/workflows/test.yml) is the authoritative suite — it
runs the real composite action on a runner through every case above, including the
expected failures.

The macOS smoke job is skipped by default so pull requests do not pay for macOS minutes.
Label a PR **`ci:macos`** to run it — worth doing for any change to runner-facing
behaviour, since that job is what backs the macOS support claim above.

[`.github/dependabot.yml`](.github/dependabot.yml) keeps the actions referenced with
`uses:` up to date, weekly. Minor and patch bumps are grouped into one PR; majors get
their own. There is no other ecosystem configured because there is nothing else to track —
the action ships no package dependencies, and PyYAML comes from the runner image.

## License

MIT — see [LICENSE](LICENSE).

[am]: https://github.com/dawg-io
