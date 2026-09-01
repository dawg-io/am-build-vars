#!/usr/bin/env python3
"""Helpers shared by the am-build-vars scripts.

`resolve.py` merges configuration layers and never touches the network;
`store.py` fetches the shared variable store and does nothing else. The few
things both of them need live here so there is exactly one definition of each.

Nothing in this module prints a resolved value. That rule holds across every
script: key names and where they came from are logged, the values are not.
"""

import datetime
import hashlib
import json
import re
import secrets
import sys

# The file inside the store artifact. One artifact, one file, one known name --
# so the download path can refuse to read anything else out of the zip.
STORE_FILENAME = "am-build-vars-store.json"

# Bumped only when the on-disk shape changes incompatibly. An older action
# refuses a newer store rather than silently misreading it.
STORE_SCHEMA = 1

ARTIFACT_PREFIX = "am-build-vars-store-"

# Serialised size ceiling for the store, in both directions. The store rides in
# environment variables at the far end, so a runaway value is a problem long
# before the artifact size limit is.
MAX_STORE_BYTES = 512 * 1024

# An artifact name may not contain " : < > | * ? \ / or carriage returns, and a
# branch name routinely contains a slash. Anything outside this set is replaced.
_ARTIFACT_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Artifact names have a length ceiling; stay well clear of it.
_ARTIFACT_NAME_MAX = 190


def fail(message, file=None):
    """Emit a GitHub Actions error annotation and exit non-zero."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    prefix = "::error file={}::".format(file.replace(",", "%2C")) if file else "::error::"
    print(prefix + escaped, flush=True)
    sys.exit(1)


def truthy(value):
    return str(value).strip().lower() in ("true", "1", "yes", "on")


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


def now_iso():
    """UTC, second resolution, with a trailing Z rather than +00:00."""
    stamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return stamp.isoformat().replace("+00:00", "Z")


def artifact_name(scope):
    """Map a sharing scope to the artifact name that holds its store.

    Scopes are branch names by default, so they contain characters an artifact
    name cannot. Slugging alone would let two different scopes land on one
    artifact -- 'feat/x' and 'feat-x' would share a store, and a value set on one
    branch would surface on the other. A short digest of the original scope is
    appended whenever slugging changed anything, which keeps the common case
    ('main') readable in the Actions UI and the rest of the cases distinct.
    """
    scope = (scope or "").strip()
    if not scope:
        fail(
            "'share-scope' resolved to an empty string. It names the store to read "
            "and write, so it cannot be empty -- set it explicitly, or leave it at "
            "its default of the current ref name."
        )
    slug = _ARTIFACT_UNSAFE.sub("-", scope)
    budget = _ARTIFACT_NAME_MAX - len(ARTIFACT_PREFIX)
    if slug != scope or len(slug) > budget:
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:8]
        slug = "{}-{}".format(slug[: budget - 9], digest)
    return ARTIFACT_PREFIX + slug


def read_store(path):
    """Parse a store file, returning (values, origins).

    A store that cannot be trusted is an error rather than a silent fall-through
    to an empty one: the caller asked for values that a previous run published,
    and quietly building without them is the confusing outcome.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        fail("Could not read the shared store at {}: {}".format(path, exc.strerror or exc))

    if len(raw.encode("utf-8")) > MAX_STORE_BYTES:
        fail(
            "The shared store at {} is larger than the {} KiB limit. Delete the "
            "store artifact to reset it.".format(path, MAX_STORE_BYTES // 1024)
        )

    try:
        data = json.loads(raw)
    except ValueError as exc:
        fail(
            "The shared store at {} is not valid JSON ({}). Delete the store "
            "artifact to reset it.".format(path, exc)
        )

    if not isinstance(data, dict):
        fail("The shared store at {} must contain a JSON object.".format(path))

    schema = data.get("schema")
    if schema != STORE_SCHEMA:
        fail(
            "The shared store at {} declares schema {!r}, but this version of "
            "am-build-vars reads schema {}. Upgrade the action, or delete the store "
            "artifact to reset it.".format(path, schema, STORE_SCHEMA)
        )

    values = data.get("values") or {}
    if not isinstance(values, dict):
        fail("The 'values' entry in the shared store at {} must be an object.".format(path))
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            fail(
                "The shared store at {} contains a non-string entry. Every stored "
                "value is a rendered string; delete the store artifact to reset "
                "it.".format(path)
            )

    origins = data.get("origins") or {}
    if not isinstance(origins, dict):
        origins = {}
    return values, origins


def dump_store(scope, values, origins):
    """Serialise a store, refusing one that has grown past the size ceiling."""
    payload = json.dumps(
        {
            "schema": STORE_SCHEMA,
            "scope": scope,
            "updated_at": now_iso(),
            "values": values,
            "origins": {key: origins[key] for key in sorted(origins) if key in values},
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > MAX_STORE_BYTES:
        fail(
            "The shared store would be larger than the {} KiB limit with these "
            "values. Share fewer or smaller values -- the store is meant for build "
            "coordinates such as tags and versions, not payloads.".format(
                MAX_STORE_BYTES // 1024
            )
        )
    return payload
