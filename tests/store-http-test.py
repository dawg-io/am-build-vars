#!/usr/bin/env python3
"""Exercise store.py's network path against a local stand-in for the API.

The rest of the suite hands the resolver a store that is already on disk. This is
the half that puts it there: the artifact listing, the redirect to signed storage,
the zip, and the file that lands in RUNNER_TEMP. A throwaway HTTP server on
localhost stands in for GitHub, so this needs no runner, no token and no network.

The assertion that matters most is the one about the redirect: the token goes to
the API and must not follow the redirect to storage, which is a different host.

Usage: tests/store-http-test.py
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
REPO = "acme/widgets"
STORE = {
    "schema": 1,
    "scope": "main",
    "updated_at": "2026-01-01T00:00:00Z",
    "values": {"image_tag": "sha-abc1234"},
    "origins": {"image_tag": {"run_id": "300"}},
}

PASS = 0
FAIL = 0


def ok(name, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print("  ok   {}".format(name))
    else:
        FAIL += 1
        print("  FAIL {}\n       expected: {!r}\n       actual:   {!r}".format(name, expected, actual))


def zip_bytes(payload):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("am-build-vars-store.json", json.dumps(payload))
    return buffer.getvalue()


class Handler(BaseHTTPRequestHandler):
    # Set per scenario by serve().
    listing = None
    listing_status = 200
    seen = None

    def log_message(self, *args):  # keep the test output readable
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        auth = self.headers.get("Authorization")
        Handler.seen.append((path, auth))

        if path == "/repos/{}/actions/artifacts".format(REPO):
            if Handler.listing_status != 200:
                self.send_response(Handler.listing_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"message":"Resource not accessible by integration"}')
                return
            body = json.dumps(Handler.listing).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.endswith("/zip"):
            # Exactly what the API does: hand back a signed URL somewhere else.
            self.send_response(302)
            self.send_header("Location", "http://{}/signed-storage/blob".format(self.headers["Host"]))
            self.end_headers()
            return

        if path == "/signed-storage/blob":
            body = zip_bytes(STORE)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


def run_store(port, workdir):
    """Run store.py against the local server, returning (status, log, outputs)."""
    output = os.path.join(workdir, "output")
    open(output, "w").close()
    env = dict(os.environ)
    env.update(
        {
            "GITHUB_OUTPUT": output,
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_API_URL": "http://127.0.0.1:{}".format(port),
            "GITHUB_RUN_ID": "999",
            "RUNNER_TEMP": workdir,
            "INPUT_SHARE_SCOPE": "main",
            "INPUT_SHARE_TOKEN": "test-token-must-not-leak",
            # urllib honours these, which is right on a proxied self-hosted
            # runner and wrong for a loopback server.
            "no_proxy": "*",
            "NO_PROXY": "*",
        }
    )
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "store.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    values = {}
    lines = open(output, encoding="utf-8").read().split("\n")
    index = 0
    while index < len(lines):
        if "<<" in lines[index]:
            key, delimiter = lines[index].split("<<", 1)
            body = []
            index += 1
            while index < len(lines) and lines[index] != delimiter:
                body.append(lines[index])
                index += 1
            values[key] = "\n".join(body)
        index += 1
    return completed.returncode, completed.stdout.decode(), values


def main():
    with open(os.path.join(FIXTURES, "artifacts-list.json"), encoding="utf-8") as handle:
        fixture = json.load(handle)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        print("  a store that exists is located, downloaded and unpacked")
        Handler.listing, Handler.listing_status, Handler.seen = fixture, 200, []
        with tempfile.TemporaryDirectory() as workdir:
            status, log, out = run_store(port, workdir)
            ok("exit status", status, 0)
            ok("found", out.get("found"), "true")
            # Artifact 5, not the newer fork-run or expired ones.
            ok("picked the newest trusted artifact", out.get("artifact-id"), "5")
            ok("provenance", out.get("run-id"), "300")
            ok("artifact name", out.get("artifact-name"), "am-build-vars-store-main")
            path = out.get("store-path") or ""
            ok("store landed under RUNNER_TEMP", path.startswith(workdir), True)
            values = json.load(open(path, encoding="utf-8"))["values"] if os.path.isfile(path) else {}
            ok("store contents round-tripped", values, {"image_tag": "sha-abc1234"})
            ok("log names the artifact", "am-build-vars-store-main" in log, True)

            requests = dict(Handler.seen)
            listing_auth = requests.get("/repos/{}/actions/artifacts".format(REPO))
            ok("the API call is authenticated", listing_auth, "Bearer test-token-must-not-leak")
            # The whole point of not following the redirect with urllib's default
            # handler: storage is a different host and gets no credentials.
            ok("the redirect target gets no token", requests.get("/signed-storage/blob"), None)

        print("  no store for this scope is a success, not an error")
        Handler.listing, Handler.listing_status, Handler.seen = {"artifacts": []}, 200, []
        with tempfile.TemporaryDirectory() as workdir:
            status, log, out = run_store(port, workdir)
            ok("exit status", status, 0)
            ok("found", out.get("found"), "false")
            ok("no path", out.get("store-path"), "")
            ok("log explains", "no shared store yet" in log, True)

        print("  a token without actions: read fails with a message naming it")
        Handler.listing, Handler.listing_status, Handler.seen = {}, 403, []
        with tempfile.TemporaryDirectory() as workdir:
            status, log, out = run_store(port, workdir)
            ok("exit status", status, 1)
            ok("names the permission", "actions: read" in log, True)
            ok("does not leak the token", "test-token-must-not-leak" not in log, True)
    finally:
        server.shutdown()

    print()
    print("{} passed, {} failed".format(PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
