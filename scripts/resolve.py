#!/usr/bin/env python3
"""Resolve per-repo build variables for the am-build-vars action.

Merges up to four layers and emits the result to $GITHUB_OUTPUT and $GITHUB_ENV.
Lowest precedence first:

    1. defaults      the workflow's inline fleet-wide defaults
    2. shared        values a previous run published, when load-shared is on
    3. file          the repository's committed am-build-vars.yml
    4. share         values this very step is publishing

The committed file outranks the shared store on purpose: config a team committed
should never lose to an artifact some run left behind months ago. What this step
publishes outranks everything, so the value written to the store and the value
the rest of the job sees can never disagree.

This script never touches the network. Finding the store is store.py's job; by
the time this runs the store is either a file on disk or nothing at all, which
is what keeps the whole merge testable without a runner.

Contract with action.yml (all values arrive as environment variables):

    INPUT_CONFIG_FILE      path to the config file, relative to the workspace
    INPUT_DEFAULTS         inline YAML mapping of fleet-wide defaults
    INPUT_EXPORT_ENV       "true" to also write each key to $GITHUB_ENV
    INPUT_FAIL_ON_MISSING  "true" to fail when the config file is absent
    INPUT_SHARE            inline YAML mapping of values to publish
    INPUT_SHARE_ENV        names of environment variables to capture and publish
    INPUT_LOAD_SHARED      "true" to apply the shared store as layer 2
    INPUT_SHARE_SCOPE      sharing scope, naming the store to write

    AM_BUILD_VARS_STORE_IN      path to the store store.py downloaded, or empty
    AM_BUILD_VARS_STORE_OUT     directory to write the outgoing store into
    AM_BUILD_VARS_STORE_RUN_ID  the run the incoming store came from
    AM_BUILD_VARS_STORE_OUTCOME the fetch step's outcome, so a fetch that was
                                skipped while sharing is on is caught here

Values are never printed. Key names and their source are, so a run is
auditable without leaking anything the config file happens to contain.
"""

import datetime
import json
import os
import re
import sys
import tempfile

from common import (
    STORE_FILENAME,
    artifact_name,
    dump_store,
    fail,
    now_iso,
    read_store,
    truthy,
    write_kv,
)

try:
    import yaml
except ImportError:  # pragma: no cover - the guard step in action.yml catches this
    print(
        "::error::am-build-vars requires PyYAML. It is preinstalled on GitHub-hosted "
        "ubuntu runner images, but not on macOS runners or most self-hosted ones. "
        "Install it before this step with 'python3 -m pip install pyyaml'.",
        flush=True,
    )
    sys.exit(1)

CONFIG_NAME = "am-build-vars.yml"

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_PREFIXES = ("GITHUB_", "ACTIONS_", "RUNNER_")
RESERVED_NAMES = ("PATH", "HOME", "CI", "NODE_OPTIONS", "LD_PRELOAD")

SHARE_ORIGIN = "the 'share' input"
SHARE_ENV_ORIGIN = "the 'share-env' input"
STORE_ORIGIN = "the shared store"

# A bad key in an input is fixed by editing the workflow. A bad key in the store
# is baked into an artifact, and it fails every step on that scope -- producers
# included, because publishing reads the store it merges into. Publishing over it
# is therefore not a way out, so say what the way out is.
STORE_REMEDY = (
    " This key is in the store artifact itself, so every step on this scope fails "
    "until it goes: delete the store artifact for this 'share-scope' to reset it."
)


def _remedy(origin):
    return STORE_REMEDY if origin == STORE_ORIGIN else ""


def load_mapping(text, origin, file=None):
    """Parse YAML text and require a mapping (or nothing) at the top level."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        fail("Malformed YAML in {}: {}".format(origin, detail), file=file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        fail(
            "{} must contain a YAML mapping of key/value pairs at the top level, "
            "found {}.".format(origin, type(data).__name__),
            file=file,
        )
    return data


def render(key, value, origin):
    """Convert a parsed YAML value into the string form written to outputs."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=False, default=str)
    fail(
        "Key '{}' in {} has an unsupported value type ({}). Supported types are "
        "strings, numbers, booleans, null, lists and mappings.".format(
            key, origin, type(value).__name__
        )
    )


def validate_key(key, origin):
    if not isinstance(key, str):
        fail(
            "{} contains a non-string key ({!r}). Keys must be plain strings.".format(
                origin, key
            )
        )
    if not KEY_PATTERN.match(key):
        fail(
            "Invalid key '{}' in {}. Keys must match [A-Za-z_][A-Za-z0-9_]* so that "
            "the JSON key and the exported environment variable name are always "
            "identical. Use underscores instead of dashes or dots.{}".format(
                key, origin, _remedy(origin)
            )
        )
    upper = key.upper()
    if upper.startswith(RESERVED_PREFIXES) or upper in RESERVED_NAMES:
        fail(
            "Key '{}' in {} is reserved. Keys that collide with runner-owned "
            "environment variables (GITHUB_*, ACTIONS_*, RUNNER_*, PATH, HOME, CI, "
            "NODE_OPTIONS, LD_PRELOAD) are rejected because exporting them would "
            "break the job.{}".format(key, origin, _remedy(origin))
        )


def render_layer(values, origin):
    """Validate and render a whole layer of parsed YAML in one go."""
    rendered = {}
    for key in values:
        validate_key(key, origin)
        rendered[key] = render(key, values[key], origin)
    return rendered


def collect_share(share_input, share_env_input):
    """Build the layer this step is publishing, from both of its inputs.

    'share' takes a YAML mapping, which is right for literals. 'share-env' takes
    names and reads the values straight out of the environment, which is right
    for anything computed during the run: a tag containing a quote, a newline or
    a brace never has to survive a trip through YAML.
    """
    values = render_layer(load_mapping(share_input, SHARE_ORIGIN), SHARE_ORIGIN)

    # Applied second, so a name given to both wins here. Documented, and the
    # unsurprising reading of "capture whatever this variable holds now".
    for name in re.split(r"[\s,]+", (share_env_input or "").strip()):
        if not name:
            continue
        validate_key(name, SHARE_ENV_ORIGIN)
        if name not in os.environ:
            fail(
                "'share-env' names '{}', but no environment variable of that name is "
                "set in this step. Set it in an earlier step (an 'echo {}=... >> "
                "$GITHUB_ENV' line, or an 'env:' block) before sharing it.".format(
                    name, name
                )
            )
        values[name] = os.environ[name]
    return values


def discover_config(config_input, workspace):
    """Find the committed config file. Returns (path_or_None, candidates)."""
    # One filename, several allowed homes. An explicit 'config-file' input turns
    # discovery off entirely and uses exactly the path given.
    if config_input:
        candidates = [config_input]
        explicit = True
    else:
        candidates = [CONFIG_NAME, os.path.join(".github", CONFIG_NAME)]
        explicit = False

    found = []
    for candidate in candidates:
        path = candidate
        if not os.path.isabs(path):
            path = os.path.join(workspace, path)
        found.append((candidate, os.path.normpath(path)))

    present = [(rel, path) for rel, path in found if os.path.isfile(path)]

    # Two homes for the same file would make precedence silent and arbitrary.
    if len(present) > 1:
        fail(
            "Found {} in more than one location: {}. Keep exactly one and delete "
            "the rest.".format(CONFIG_NAME, ", ".join(rel for rel, _ in present))
        )

    # A near-miss is worth an error rather than a silent fall-through to defaults:
    # the repository plainly meant to configure something, and quietly building
    # with fleet defaults instead is the confusing outcome.
    if not present and not explicit:
        for rel, path in found:
            for wrong in (path[: -len(".yml")] + ".yaml", path + ".yaml"):
                if os.path.isfile(wrong):
                    fail(
                        "Found {} but this action reads {} only. Rename it.".format(
                            os.path.relpath(wrong, workspace), CONFIG_NAME
                        )
                    )

    return (present[0][1] if present else None), found


def publish(scope, store_values, store_origins, share_values, destination):
    """Write the outgoing store, merging what this step shares into what exists.

    Merging rather than replacing is the whole point: a tag published by the
    build workflow and an environment published by the deploy workflow both have
    to survive, or the second producer silently erases the first.
    """
    merged = dict(store_values)
    merged.update(share_values)

    stamp = {
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "at": now_iso(),
    }
    origins = dict(store_origins)
    for key in share_values:
        origins[key] = stamp

    payload = dump_store(scope, merged, origins)

    directory = destination or tempfile.mkdtemp(prefix="am-build-vars-store-")
    try:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, STORE_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        fail("Could not write the shared store to {}: {}".format(directory, exc.strerror or exc))
    return path, merged


def main():
    config_input = os.environ.get("INPUT_CONFIG_FILE", "").strip()
    defaults_input = os.environ.get("INPUT_DEFAULTS", "")
    export_env = truthy(os.environ.get("INPUT_EXPORT_ENV", "true"))
    fail_on_missing = truthy(os.environ.get("INPUT_FAIL_ON_MISSING", "false"))
    share_input = os.environ.get("INPUT_SHARE", "")
    share_env_input = os.environ.get("INPUT_SHARE_ENV", "")
    load_shared = truthy(os.environ.get("INPUT_LOAD_SHARED", "false"))
    share_scope = os.environ.get("INPUT_SHARE_SCOPE", "")

    store_in = os.environ.get("AM_BUILD_VARS_STORE_IN", "").strip()
    store_out = os.environ.get("AM_BUILD_VARS_STORE_OUT", "").strip()
    store_run_id = os.environ.get("AM_BUILD_VARS_STORE_RUN_ID", "").strip()
    store_outcome = os.environ.get("AM_BUILD_VARS_STORE_OUTCOME", "").strip()

    github_output = os.environ.get("GITHUB_OUTPUT")
    github_env = os.environ.get("GITHUB_ENV")
    workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()

    if not github_output:
        fail("GITHUB_OUTPUT is not set. This action must run inside GitHub Actions.")
    if export_env and not github_env:
        fail("GITHUB_ENV is not set. This action must run inside GitHub Actions.")

    # The composite gates the fetch step on a workflow expression, and an
    # expression cannot normalise a value the way truthy() does -- there is no
    # trim() to call. So a padded value such as "load-shared: ' true '" is false
    # to the gate and true here, and the step would otherwise exit 0 having
    # silently resolved nothing. 'skipped' is the only outcome a step that did
    # not run reports; a fetch that fails aborts the composite before this.
    if store_outcome == "skipped" and (load_shared or share_input.strip() or share_env_input.strip()):
        fail(
            "This step asked for the shared store, but the composite action did "
            "not fetch it -- the value of 'load-shared', 'share' or 'share-env' "
            "was not recognised by the step's own condition. Surrounding "
            "whitespace is the usual cause: write 'load-shared: true', not "
            "'load-shared: \" true \"'. Failing here rather than resolving zero "
            "shared keys and calling it a success."
        )

    # --- defaults -----------------------------------------------------------
    defaults = render_layer(
        load_mapping(defaults_input, "the 'defaults' input"), "the 'defaults' input"
    )

    # --- values this step publishes -----------------------------------------
    share_values = collect_share(share_input, share_env_input)

    # --- the store a previous run left behind -------------------------------
    # Read whenever one was downloaded, even with load-shared off: publishing
    # merges into it, and a producer that skipped the read would erase it.
    store_values, store_origins = read_store(store_in) if store_in else ({}, {})
    if store_values:
        # An artifact is not a trusted input; hold it to the same key rules as
        # everything else, so a hand-crafted store cannot export PATH.
        for key in store_values:
            validate_key(key, STORE_ORIGIN)
    shared = dict(store_values) if load_shared else {}

    # --- config file --------------------------------------------------------
    config_path, candidates = discover_config(config_input, workspace)

    file_values = {}
    config_file_used = ""
    if config_path:
        config_file_used = os.path.relpath(config_path, workspace)
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            fail("Could not read {}: {}".format(config_file_used, exc.strerror or exc))
        file_values = render_layer(
            load_mapping(text, config_file_used, file=config_file_used), config_file_used
        )
    elif fail_on_missing:
        fail(
            "No config file found and 'fail-on-missing' is true. Looked for: "
            "{}.".format(", ".join(rel for rel, _ in candidates))
        )

    # --- merge: lowest precedence first, per top-level key (no deep merge) ---
    rendered = {}
    sources = {}
    for origin, layer in (
        ("default", defaults),
        ("shared", shared),
        ("file", file_values),
        ("share", share_values),
    ):
        for key, value in layer.items():
            rendered[key] = value
            sources[key] = origin

    # --- the outgoing store -------------------------------------------------
    store_path = ""
    store_name = ""
    if share_values:
        store_name = artifact_name(share_scope)
        store_path, _ = publish(
            share_scope, store_values, store_origins, share_values, store_out
        )

    # --- emit ---------------------------------------------------------------
    for key, value in rendered.items():
        write_kv(github_output, key, value)
        if export_env:
            write_kv(github_env, key, value)

    write_kv(github_output, "json", json.dumps(rendered, separators=(",", ":")))
    write_kv(github_output, "keys", json.dumps(sorted(rendered), separators=(",", ":")))
    write_kv(github_output, "config-file-used", config_file_used)
    write_kv(github_output, "sources", json.dumps(sources, separators=(",", ":"), sort_keys=True))
    write_kv(github_output, "shared-json", json.dumps(shared, separators=(",", ":")))
    write_kv(github_output, "shared-run-id", store_run_id if load_shared and store_in else "")
    write_kv(github_output, "should-upload", "true" if share_values else "false")
    write_kv(github_output, "share-artifact-name", store_name)
    write_kv(github_output, "share-artifact-path", store_path)

    # --- log: key names and their source only, never values ------------------
    if config_file_used:
        print("am-build-vars: read {}".format(config_file_used))
    else:
        print(
            "am-build-vars: no config file found (looked for {}); using defaults "
            "only".format(", ".join(rel for rel, _ in candidates))
        )
    if load_shared:
        if store_in:
            print(
                "am-build-vars: applied {} shared key(s) from run {}".format(
                    len(shared), store_run_id or "unknown"
                )
            )
        else:
            print("am-build-vars: no shared store to apply")
    if rendered:
        print("am-build-vars: resolved {} key(s):".format(len(rendered)))
        for key in sorted(rendered):
            print("  {} (from {})".format(key, sources[key]))
    else:
        print("am-build-vars: resolved 0 keys.")
    if export_env and rendered:
        print("am-build-vars: exported {} key(s) to the job environment.".format(len(rendered)))
    if share_values:
        print(
            "am-build-vars: publishing {} key(s) to shared store '{}':".format(
                len(share_values), store_name
            )
        )
        for key in sorted(share_values):
            print("  {}".format(key))


if __name__ == "__main__":
    main()
