#!/usr/bin/env python3
"""Resolve per-repo build variables for the am-build-vars action.

Reads a committed YAML config file plus an inline YAML `defaults` mapping,
merges them (file wins, per top-level key), and emits the result to
$GITHUB_OUTPUT and $GITHUB_ENV.

Contract with action.yml (all values arrive as environment variables):

    INPUT_CONFIG_FILE      path to the config file, relative to the workspace
    INPUT_DEFAULTS         inline YAML mapping of fleet-wide defaults
    INPUT_EXPORT_ENV       "true" to also write each key to $GITHUB_ENV
    INPUT_FAIL_ON_MISSING  "true" to fail when the config file is absent

Values are never printed. Key names and their source are, so a run is
auditable without leaking anything the config file happens to contain.
"""

import datetime
import json
import os
import re
import secrets
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the guard step in action.yml catches this
    print(
        "::error::am-build-vars requires PyYAML. Install it on this runner with "
        "'python3 -m pip install --user pyyaml'. It is preinstalled on all "
        "GitHub-hosted ubuntu and macOS runner images.",
        flush=True,
    )
    sys.exit(1)

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_PREFIXES = ("GITHUB_", "ACTIONS_", "RUNNER_")
RESERVED_NAMES = ("PATH", "HOME", "CI", "NODE_OPTIONS", "LD_PRELOAD")


def fail(message, file=None):
    """Emit a GitHub Actions error annotation and exit non-zero."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    prefix = "::error file={}::".format(file.replace(",", "%2C")) if file else "::error::"
    print(prefix + escaped, flush=True)
    sys.exit(1)


def truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes", "on")


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
            "identical. Use underscores instead of dashes or dots.".format(key, origin)
        )
    upper = key.upper()
    if upper.startswith(RESERVED_PREFIXES) or upper in RESERVED_NAMES:
        fail(
            "Key '{}' in {} is reserved. Keys that collide with runner-owned "
            "environment variables (GITHUB_*, ACTIONS_*, RUNNER_*, PATH, HOME, CI, "
            "NODE_OPTIONS, LD_PRELOAD) are rejected because exporting them would "
            "break the job.".format(key, origin)
        )


def write_kv(path, key, value):
    """Append key=value using the heredoc delimiter form.

    The delimiter is random and re-rolled if it ever appears inside the value,
    which is what makes newlines and shell metacharacters safe.
    """
    delimiter = "ghadelimiter_" + secrets.token_hex(16)
    while delimiter in value or delimiter in key:
        delimiter = "ghadelimiter_" + secrets.token_hex(16)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{}<<{}\n{}\n{}\n".format(key, delimiter, value, delimiter))


def main():
    config_input = os.environ.get("INPUT_CONFIG_FILE", "").strip()
    defaults_input = os.environ.get("INPUT_DEFAULTS", "")
    export_env = truthy(os.environ.get("INPUT_EXPORT_ENV", "true"))
    fail_on_missing = truthy(os.environ.get("INPUT_FAIL_ON_MISSING", "false"))

    github_output = os.environ.get("GITHUB_OUTPUT")
    github_env = os.environ.get("GITHUB_ENV")
    workspace = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()

    if not github_output:
        fail("GITHUB_OUTPUT is not set. This action must run inside GitHub Actions.")
    if export_env and not github_env:
        fail("GITHUB_ENV is not set. This action must run inside GitHub Actions.")

    if not config_input:
        fail("The 'config-file' input must not be empty.")

    # --- defaults -----------------------------------------------------------
    defaults = load_mapping(defaults_input, "the 'defaults' input")
    for key in defaults:
        validate_key(key, "the 'defaults' input")

    # --- config file --------------------------------------------------------
    config_path = config_input
    if not os.path.isabs(config_path):
        config_path = os.path.join(workspace, config_path)
    config_path = os.path.normpath(config_path)

    # A repo must not carry both spellings of the same config file: the choice of
    # which one wins would be silent and arbitrary. This applies to whatever path
    # is configured, not just the default one.
    sibling = None
    if config_path.endswith(".yml"):
        sibling = config_path[: -len(".yml")] + ".yaml"
    elif config_path.endswith(".yaml"):
        sibling = config_path[: -len(".yaml")] + ".yml"
    if sibling and os.path.isfile(config_path) and os.path.isfile(sibling):
        first, second = sorted([config_path, sibling])
        fail(
            "Found both {} and {}. Keep exactly one of them and delete the "
            "other.".format(os.path.relpath(first, workspace), os.path.relpath(second, workspace))
        )

    file_values = {}
    config_file_used = ""
    if os.path.isfile(config_path):
        display = os.path.relpath(config_path, workspace)
        config_file_used = display
        try:
            with open(config_path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            fail("Could not read {}: {}".format(display, exc.strerror or exc))
        file_values = load_mapping(text, display, file=display)
        for key in file_values:
            validate_key(key, display)
    elif fail_on_missing:
        fail(
            "Config file {} not found and 'fail-on-missing' is true.".format(
                os.path.relpath(config_path, workspace)
            )
        )

    # --- merge: file wins, per top-level key (no deep merge) -----------------
    resolved = dict(defaults)
    resolved.update(file_values)

    rendered = {}
    sources = {}
    for key in resolved:
        origin = config_file_used if key in file_values else "the 'defaults' input"
        rendered[key] = render(key, resolved[key], origin)
        sources[key] = "file" if key in file_values else "default"

    # --- emit ---------------------------------------------------------------
    for key, value in rendered.items():
        write_kv(github_output, key, value)
        if export_env:
            write_kv(github_env, key, value)

    write_kv(github_output, "json", json.dumps(rendered, separators=(",", ":")))
    write_kv(github_output, "keys", json.dumps(sorted(rendered), separators=(",", ":")))
    write_kv(github_output, "config-file-used", config_file_used)

    # --- log: key names and their source only, never values ------------------
    if config_file_used:
        print("am-build-vars: read {}".format(config_file_used))
    else:
        print(
            "am-build-vars: no config file at {} (using defaults only)".format(
                os.path.relpath(config_path, workspace)
            )
        )
    if rendered:
        print("am-build-vars: resolved {} key(s):".format(len(rendered)))
        for key in sorted(rendered):
            print("  {} (from {})".format(key, sources[key]))
    else:
        print("am-build-vars: resolved 0 keys.")
    if export_env and rendered:
        print("am-build-vars: exported {} key(s) to the job environment.".format(len(rendered)))


if __name__ == "__main__":
    main()
