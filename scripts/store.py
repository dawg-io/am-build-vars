#!/usr/bin/env python3
"""Locate and download the shared variable store for the am-build-vars action.

The store is an ordinary Actions artifact holding one JSON file. Writing it is
`actions/upload-artifact`'s job; this script is the read half, because the thing
a caller actually wants -- "the newest store for this scope, wherever it was
written" -- is a repository-wide lookup that download-artifact cannot express: it
downloads by run, and the run that wrote the value is exactly what the caller does
not know.

Contract with action.yml (all values arrive as environment variables):

    INPUT_SHARE_SCOPE  sharing scope; maps to exactly one artifact name
    INPUT_SHARE_TOKEN  token for the artifact lookup, needing actions: read

Step outputs, written to $GITHUB_OUTPUT:

    found          "true" when a store was downloaded
    run-id         the run that wrote it, for provenance
    artifact-id    the artifact it came from
    artifact-name  the artifact name that was searched for
    store-path     where the JSON landed on disk

Finding nothing is a success: the first run in a new scope has no store yet.
"""

import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from common import MAX_STORE_BYTES, STORE_FILENAME, artifact_name, fail, write_kv

API_VERSION = "2022-11-28"
USER_AGENT = "am-build-vars"
PER_PAGE = 100
MAX_PAGES = 5
RETRIES = 3
TIMEOUT = 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from following the artifact download redirect.

    The 302 points at signed blob storage on a different host, and urllib would
    replay the Authorization header there. The redirect is followed by hand
    below, with no credentials attached.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url, token=None, accept="application/vnd.github+json", allow_redirect=True):
    request = urllib.request.Request(url)
    request.add_header("Accept", accept)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("X-GitHub-Api-Version", API_VERSION)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener() if allow_redirect else urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=TIMEOUT) as response:
        return response.read()


def _retrying(action, what):
    """Run a request, retrying only what is worth retrying.

    A 403 here is nearly always a missing permission rather than a blip, so it
    gets a message naming the fix instead of three slow attempts at the same
    refusal.
    """
    delay = 1.0
    for attempt in range(1, RETRIES + 1):
        try:
            return action()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
                if remaining == "0":
                    fail(
                        "The GitHub API rate limit is exhausted, so {} failed. Retry "
                        "the run once the limit resets.".format(what)
                    )
                fail(
                    "Not authorised to {} (HTTP {}). Sharing variables reads the "
                    "repository's artifacts, so the job needs 'actions: read' in its "
                    "permissions block, and 'share-token' must be a token that has "
                    "it.".format(what, exc.code)
                )
            if exc.code == 404:
                raise
            if exc.code < 500 or attempt == RETRIES:
                fail("Could not {} (HTTP {}).".format(what, exc.code))
        except (urllib.error.URLError, OSError) as exc:
            if attempt == RETRIES:
                fail("Could not {}: {}.".format(what, getattr(exc, "reason", exc)))
        time.sleep(delay)
        delay *= 2


def list_artifacts(api_url, repository, name, token):
    """Page through the repository's artifacts, newest pages first.

    The name filter is a server-side optimisation only: choose_artifact filters
    again, so an API that ignores the parameter still yields a correct answer.
    """
    collected = []
    for page in range(1, MAX_PAGES + 1):
        url = "{}/repos/{}/actions/artifacts?{}".format(
            api_url,
            repository,
            urllib.parse.urlencode({"name": name, "per_page": PER_PAGE, "page": page}),
        )
        try:
            body = _retrying(lambda: _get(url, token), "list the repository's artifacts")
        except urllib.error.HTTPError:
            break
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            fail("The artifact listing API returned a response that is not JSON.")
        batch = payload.get("artifacts") or []
        collected.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return collected


def choose_artifact(artifacts, name, current_run_id):
    """Pick the newest store artifact this run is allowed to trust.

    An artifact qualifies when it still exists, carries exactly the name being
    looked for, and was produced either by a run of this repository itself or by
    the current run.

    That second clause is what lets a pull request from a fork share values
    between its own jobs. The first is what stops a fork's run from feeding
    values into any *later* run of the base repository -- the shape of the
    pull_request_target and workflow_run privilege escalations, where a job with
    a write token consumes something a fork wrote.
    """
    eligible = []
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("name") != name or artifact.get("expired"):
            continue
        run = artifact.get("workflow_run")
        if not isinstance(run, dict):
            continue
        repo_id = run.get("repository_id")
        same_repo = repo_id is not None and repo_id == run.get("head_repository_id")
        this_run = bool(current_run_id) and str(run.get("id")) == str(current_run_id)
        if not (same_repo or this_run):
            continue
        eligible.append(artifact)

    if not eligible:
        return None
    eligible.sort(
        key=lambda a: (str(a.get("created_at") or ""), int(a.get("id") or 0)), reverse=True
    )
    return eligible[0]


def download(api_url, repository, artifact_id, token):
    """Fetch one artifact's zip and return the store file inside it."""
    url = "{}/repos/{}/actions/artifacts/{}/zip".format(api_url, repository, artifact_id)
    what = "download the shared store artifact"

    def fetch():
        try:
            return _get(url, token, accept="application/vnd.github+json", allow_redirect=False)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers.get("Location") if exc.headers else None
            if not location:
                fail("The artifact download returned a redirect with no location.")
            # Deliberately unauthenticated: the location is a pre-signed URL on
            # storage that is not GitHub, and the token has no business there.
            return _get(location, token=None, accept="*/*")

    body = _retrying(fetch, what)

    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        fail("The shared store artifact is not a readable zip archive.")

    with archive:
        try:
            info = archive.getinfo(STORE_FILENAME)
        except KeyError:
            fail(
                "The artifact does not contain {}. An artifact of that name that "
                "am-build-vars did not write is in the way -- rename it, or pick a "
                "different 'share-scope'.".format(STORE_FILENAME)
            )
        if info.file_size > MAX_STORE_BYTES:
            fail(
                "The shared store is larger than the {} KiB limit. Delete the store "
                "artifact to reset it.".format(MAX_STORE_BYTES // 1024)
            )
        return archive.read(STORE_FILENAME)


def main():
    scope = os.environ.get("INPUT_SHARE_SCOPE", "")
    token = os.environ.get("INPUT_SHARE_TOKEN", "").strip()
    github_output = os.environ.get("GITHUB_OUTPUT")
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    api_url = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    current_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    runner_temp = os.environ.get("RUNNER_TEMP") or os.getcwd()

    if not github_output:
        fail("GITHUB_OUTPUT is not set. This action must run inside GitHub Actions.")
    if not repository:
        fail("GITHUB_REPOSITORY is not set. This action must run inside GitHub Actions.")
    if not token:
        fail(
            "Sharing variables needs a token to look the store artifact up. Leave "
            "'share-token' at its default of the job's own github.token, or pass one "
            "with 'actions: read'."
        )

    name = artifact_name(scope)
    write_kv(github_output, "artifact-name", name)

    chosen = choose_artifact(list_artifacts(api_url, repository, name, token), name, current_run_id)
    if chosen is None:
        print(
            "am-build-vars: no shared store yet for scope '{}' (artifact {})".format(scope, name)
        )
        write_kv(github_output, "found", "false")
        write_kv(github_output, "run-id", "")
        write_kv(github_output, "artifact-id", "")
        write_kv(github_output, "store-path", "")
        return

    payload = download(api_url, repository, chosen["id"], token)
    destination = os.path.join(runner_temp, "am-build-vars-store-in")
    os.makedirs(destination, exist_ok=True)
    store_path = os.path.join(destination, STORE_FILENAME)
    with open(store_path, "wb") as handle:
        handle.write(payload)

    run_id = str((chosen.get("workflow_run") or {}).get("id") or "")
    print(
        "am-build-vars: found shared store '{}' from run {} (artifact {})".format(
            name, run_id or "unknown", chosen["id"]
        )
    )
    write_kv(github_output, "found", "true")
    write_kv(github_output, "run-id", run_id)
    write_kv(github_output, "artifact-id", str(chosen["id"]))
    write_kv(github_output, "store-path", store_path)


if __name__ == "__main__":
    main()
