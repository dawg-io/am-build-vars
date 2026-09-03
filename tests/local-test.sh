#!/usr/bin/env bash
# Exercise scripts/resolve.py without a GitHub runner.
#
# The self-test workflow (.github/workflows/test.yml) is the authoritative test suite —
# it runs the real composite action. This script covers the same cases against the
# resolver directly so contributors get a fast local signal.
#
# Usage: tests/local-test.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES="$ROOT/tests/fixtures"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

# run <config-file> <defaults> [export-env] [fail-on-missing]
# Populates OUT_FILE / ENV_FILE / LOG / STATUS for the assertions below.
#
# The sharing inputs arrive through optional shell variables rather than more
# positional arguments -- SHARE, SHARE_ENV, LOAD_SHARED, SHARE_SCOPE, STORE_IN,
# STORE_OUT and STORE_RUN_ID. Call reset_share before setting them, the way the
# discovery cases below call mkws.
run() {
  OUT_FILE="$WORK/output.$RANDOM"
  ENV_FILE="$WORK/env.$RANDOM"
  : >"$OUT_FILE"
  : >"$ENV_FILE"
  LOG="$(
    PYTHONDONTWRITEBYTECODE=1 \
    GITHUB_WORKSPACE="${WS:-$ROOT}" \
    GITHUB_OUTPUT="$OUT_FILE" \
    GITHUB_ENV="$ENV_FILE" \
    INPUT_CONFIG_FILE="$1" \
    INPUT_DEFAULTS="${2:-}" \
    INPUT_EXPORT_ENV="${3:-true}" \
    INPUT_FAIL_ON_MISSING="${4:-false}" \
    INPUT_SHARE="${SHARE:-}" \
    INPUT_SHARE_ENV="${SHARE_ENV:-}" \
    INPUT_LOAD_SHARED="${LOAD_SHARED:-false}" \
    INPUT_SHARE_SCOPE="${SHARE_SCOPE:-main}" \
    GITHUB_RUN_ID="${RUN_ID:-4242}" \
    AM_BUILD_VARS_STORE_IN="${STORE_IN:-}" \
    AM_BUILD_VARS_STORE_OUT="${STORE_OUT:-}" \
    AM_BUILD_VARS_STORE_RUN_ID="${STORE_RUN_ID:-}" \
    AM_BUILD_VARS_STORE_OUTCOME="${STORE_OUTCOME:-}" \
    python3 "$ROOT/scripts/resolve.py" 2>&1
  )"
  STATUS=$?
}

# get <file> <key> — read a value back out of a $GITHUB_OUTPUT / $GITHUB_ENV file,
# honouring the heredoc delimiter form.
get() {
  python3 - "$1" "$2" <<'PY'
import re, sys
path, wanted = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().split("\n")
i = 0
while i < len(lines):
    m = re.match(r"^([^<=]+)<<(\S+)$", lines[i])
    if m:
        key, delim = m.group(1), m.group(2)
        body = []
        i += 1
        while i < len(lines) and lines[i] != delim:
            body.append(lines[i])
            i += 1
        if key == wanted:
            sys.stdout.write("\n".join(body))
            sys.exit(0)
    i += 1
sys.exit(1)
PY
}

reset_share() {
  unset SHARE SHARE_ENV LOAD_SHARED SHARE_SCOPE STORE_IN STORE_OUT STORE_RUN_ID RUN_ID STORE_OUTCOME
}

# stored <dir> <expr> — read something out of a store file the resolver wrote.
stored() {
  python3 - "$1/am-build-vars-store.json" "$2" <<'PY'
import json, sys
store = json.load(open(sys.argv[1], encoding="utf-8"))
print(eval(sys.argv[2], {"store": store, "sorted": sorted, "len": len}))
PY
}

# choose <artifact-name> <current-run-id> — the id choose_artifact settles on.
choose() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/scripts" python3 - "$FIXTURES/artifacts-list.json" "$1" "$2" <<'PY'
import json, sys, store
artifacts = json.load(open(sys.argv[1], encoding="utf-8"))["artifacts"]
picked = store.choose_artifact(artifacts, sys.argv[2], sys.argv[3])
print("" if picked is None else picked["id"])
PY
}

# name <scope> — the artifact name a scope maps to.
name() {
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT/scripts" python3 -c "import common,sys; print(common.artifact_name(sys.argv[1]))" "$1"
}

ok() {
  local name="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    PASS=$((PASS + 1))
    printf '  ok   %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf '  FAIL %s\n       expected: %q\n       actual:   %q\n' "$name" "$expected" "$actual"
  fi
}

contains() {
  local name="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*)
      PASS=$((PASS + 1))
      printf '  ok   %s\n' "$name"
      ;;
    *)
      FAIL=$((FAIL + 1))
      printf '  FAIL %s\n       %q did not contain %q\n' "$name" "$haystack" "$needle"
      ;;
  esac
}

echo "1. config file present"
run "tests/fixtures/basic.yml" ""
ok "exit status" "$STATUS" "0"
ok "string value" "$(get "$OUT_FILE" node_version)" "20"
ok "bool value" "$(get "$OUT_FILE" coverage_enabled)" "true"
ok "int value" "$(get "$OUT_FILE" retries)" "3"
ok "list -> json" "$(get "$OUT_FILE" test_matrix)" "[18,20,24]"
ok "map -> json" "$(get "$OUT_FILE" build_args)" '{"target":"production","minify":true}'
ok "env exported" "$(get "$ENV_FILE" node_version)" "20"
ok "config-file-used" "$(get "$OUT_FILE" config-file-used)" "tests/fixtures/basic.yml"
ok "keys output" "$(get "$OUT_FILE" keys)" '["build_args","coverage_enabled","node_version","retries","runner","test_matrix"]'
contains "no values in log" "$LOG" "node_version (from file)"
case "$LOG" in *ubuntu-latest*) FAIL=$((FAIL+1)); echo "  FAIL log leaked a value";; *) PASS=$((PASS+1)); echo "  ok   log leaked no values";; esac

echo "2. config file absent, defaults applied"
run "tests/fixtures/nope.yml" $'node_version: "18"\nrunner: ubuntu-22.04\n'
ok "exit status" "$STATUS" "0"
ok "default applied" "$(get "$OUT_FILE" node_version)" "18"
ok "config-file-used empty" "$(get "$OUT_FILE" config-file-used)" ""
contains "log explains" "$LOG" "no config file found"

echo "3. precedence: file wins, untouched defaults survive"
run "tests/fixtures/override.yml" $'node_version: "18"\nrunner: ubuntu-22.04\ncoverage_enabled: false\n'
ok "exit status" "$STATUS" "0"
ok "file overrides" "$(get "$OUT_FILE" node_version)" "24"
ok "default survives (runner)" "$(get "$OUT_FILE" runner)" "ubuntu-22.04"
ok "default survives (bool)" "$(get "$OUT_FILE" coverage_enabled)" "false"
ok "json shape" "$(get "$OUT_FILE" json)" '{"node_version":"24","runner":"ubuntu-22.04","coverage_enabled":"false"}'

echo "4. discovery: root, .github/, conflict, near-miss"
# A scratch workspace per case, so discovery has a realistic tree to search.
mkws() { WS="$WORK/ws.$RANDOM"; mkdir -p "$WS/.github"; }

mkws; cp "$FIXTURES/basic.yml" "$WS/am-build-vars.yml"
run "" ""
ok "root: exit status" "$STATUS" "0"
ok "root: value" "$(get "$OUT_FILE" node_version)" "20"
ok "root: path reported" "$(get "$OUT_FILE" config-file-used)" "am-build-vars.yml"

mkws; cp "$FIXTURES/basic.yml" "$WS/.github/am-build-vars.yml"
run "" ""
ok ".github: exit status" "$STATUS" "0"
ok ".github: value" "$(get "$OUT_FILE" node_version)" "20"
ok ".github: path reported" "$(get "$OUT_FILE" config-file-used)" ".github/am-build-vars.yml"

mkws; cp "$FIXTURES/basic.yml" "$WS/am-build-vars.yml"
cp "$FIXTURES/override.yml" "$WS/.github/am-build-vars.yml"
run "" ""
ok "two locations: exit status" "$STATUS" "1"
contains "two locations: names both" "$LOG" ".github/am-build-vars.yml"
contains "two locations: names both" "$LOG" "am-build-vars.yml"

mkws; cp "$FIXTURES/wrong-ext.yaml" "$WS/am-build-vars.yaml"
run "" ""
ok "wrong extension: exit status" "$STATUS" "1"
contains "wrong extension: names it" "$LOG" "am-build-vars.yaml"
contains "wrong extension: says rename" "$LOG" "Rename it"

mkws
run "" $'node_version: "18"\n'
ok "no file anywhere: exit status" "$STATUS" "0"
ok "no file anywhere: defaults" "$(get "$OUT_FILE" node_version)" "18"
ok "no file anywhere: path empty" "$(get "$OUT_FILE" config-file-used)" ""

# An explicit config-file turns discovery off: a root file must be ignored.
mkws; cp "$FIXTURES/override.yml" "$WS/am-build-vars.yml"
run "$FIXTURES/basic.yml" ""
ok "explicit path wins: exit status" "$STATUS" "0"
ok "explicit path wins: value" "$(get "$OUT_FILE" node_version)" "20"
unset WS

echo "5. malformed YAML -> failure naming the file"
run "tests/fixtures/malformed.yml" ""
ok "exit status" "$STATUS" "1"
contains "names the file" "$LOG" "tests/fixtures/malformed.yml"
contains "says malformed" "$LOG" "Malformed YAML"

echo "6. multi-line and metacharacter values round-trip"
run "tests/fixtures/multiline.yml" ""
ok "exit status" "$STATUS" "0"
# The trailing 'X' sentinel stops $(...) from swallowing the value's trailing newline,
# which a YAML '|' block scalar keeps and which must survive the round-trip.
ok "newlines preserved" "$(get "$OUT_FILE" release_notes; printf X)" \
  $'First line\nSecond line with "quotes" and $DOLLAR\nThird line\nX'
ok "metacharacters preserved" "$(get "$OUT_FILE" tricky)" 'a`b$(c)d;e&f|g'
ok "env newlines preserved" "$(get "$ENV_FILE" release_notes; printf X)" \
  $'First line\nSecond line with "quotes" and $DOLLAR\nThird line\nX'

echo "7. reserved key -> failure"
run "tests/fixtures/reserved.yml" ""
ok "exit status" "$STATUS" "1"
contains "names the key" "$LOG" "GITHUB_TOKEN"

echo "8. invalid key name -> failure"
run "tests/fixtures/bad-key.yml" ""
ok "exit status" "$STATUS" "1"
contains "names the key" "$LOG" "node-version"

echo "9. malformed defaults input -> failure"
run "tests/fixtures/nope.yml" $'node_version: "20\n  - broken\n'
ok "exit status" "$STATUS" "1"
contains "names the input" "$LOG" "defaults"

echo "10. non-mapping config -> failure"
printf -- '- one\n- two\n' >"$WORK/list.yml"
run "$WORK/list.yml" ""
ok "exit status" "$STATUS" "1"
contains "explains mapping" "$LOG" "mapping"

echo "11. export-env false leaves the environment alone"
run "tests/fixtures/basic.yml" "" "false"
ok "exit status" "$STATUS" "0"
ok "output still written" "$(get "$OUT_FILE" node_version)" "20"
ok "env file empty" "$(wc -c <"$ENV_FILE" | tr -d ' ')" "0"

echo "12. fail-on-missing"
run "tests/fixtures/nope.yml" "" "true" "true"
ok "exit status" "$STATUS" "1"
contains "explains" "$LOG" "fail-on-missing"

echo "13. empty config file is not an error"
: >"$WORK/empty.yml"
run "$WORK/empty.yml" $'node_version: "20"\n'
ok "exit status" "$STATUS" "0"
ok "defaults still apply" "$(get "$OUT_FILE" node_version)" "20"

echo "14. shared store: applied over the defaults, under the config file"
reset_share
LOAD_SHARED=true STORE_IN="$FIXTURES/store-basic.json" STORE_RUN_ID=100
run "$FIXTURES/override.yml" $'node_version: "18"\nimage_tag: from-default\nrunner: ubuntu-22.04\n'
ok "exit status" "$STATUS" "0"
# The committed file outranks the store, so a pinned key stays pinned.
ok "file beats shared" "$(get "$OUT_FILE" node_version)" "24"
# ...and the store outranks the inline default.
ok "shared beats default" "$(get "$OUT_FILE" image_tag)" "sha-abc1234"
ok "untouched default survives" "$(get "$OUT_FILE" runner)" "ubuntu-22.04"
ok "shared key exported to env" "$(get "$ENV_FILE" image_tag)" "sha-abc1234"
ok "sources" "$(get "$OUT_FILE" sources)" \
  '{"image_tag":"shared","node_version":"file","runner":"default"}'
ok "shared-json is the shared layer alone" "$(get "$OUT_FILE" shared-json)" \
  '{"image_tag":"sha-abc1234","node_version":"22"}'
ok "provenance" "$(get "$OUT_FILE" shared-run-id)" "100"
ok "nothing to upload" "$(get "$OUT_FILE" should-upload)" "false"
contains "log names the source" "$LOG" "image_tag (from shared)"
case "$LOG" in *sha-abc1234*) FAIL=$((FAIL+1)); echo "  FAIL log leaked a shared value";; *) PASS=$((PASS+1)); echo "  ok   log leaked no shared values";; esac

echo "15. load-shared off ignores a store that is sitting right there"
reset_share
STORE_IN="$FIXTURES/store-basic.json" STORE_RUN_ID=100
run "$FIXTURES/override.yml" $'image_tag: from-default\n'
ok "exit status" "$STATUS" "0"
ok "store not applied" "$(get "$OUT_FILE" image_tag)" "from-default"
ok "shared-json empty" "$(get "$OUT_FILE" shared-json)" "{}"
ok "no provenance" "$(get "$OUT_FILE" shared-run-id)" ""

echo "16. share: published values outrank every other layer"
reset_share
STORE_OUT="$WORK/out16" SHARE=$'node_version: "26"\nimage_tag: sha-new\n'
run "$FIXTURES/override.yml" $'node_version: "18"\n'
ok "exit status" "$STATUS" "0"
ok "share beats the file" "$(get "$OUT_FILE" node_version)" "26"
ok "share is exported like any key" "$(get "$ENV_FILE" image_tag)" "sha-new"
ok "sources" "$(get "$OUT_FILE" sources)" '{"image_tag":"share","node_version":"share"}'
ok "upload requested" "$(get "$OUT_FILE" should-upload)" "true"
ok "artifact name" "$(get "$OUT_FILE" share-artifact-name)" "am-build-vars-store-main"
ok "artifact path" "$(get "$OUT_FILE" share-artifact-path)" "$WORK/out16/am-build-vars-store.json"
ok "value reached the store" "$(stored "$WORK/out16" 'store["values"]["image_tag"]')" "sha-new"
ok "store records the scope" "$(stored "$WORK/out16" 'store["scope"]')" "main"
ok "store records provenance" "$(stored "$WORK/out16" 'store["origins"]["image_tag"]["run_id"]')" "4242"

echo "17. publishing merges into the store rather than replacing it"
# load-shared is off here on purpose: a pure producer must still not erase what
# another workflow published, or the second producer wins and the first vanishes.
reset_share
STORE_IN="$FIXTURES/store-basic.json" STORE_OUT="$WORK/out17" SHARE=$'deploy_target: staging\n'
run "$FIXTURES/nope.yml" ""
ok "exit status" "$STATUS" "0"
ok "old and new keys both survive" "$(stored "$WORK/out17" '",".join(sorted(store["values"]))')" \
  "deploy_target,image_tag,node_version"
ok "an existing value is untouched" "$(stored "$WORK/out17" 'store["values"]["image_tag"]')" "sha-abc1234"
ok "an existing origin is untouched" "$(stored "$WORK/out17" 'store["origins"]["image_tag"]["run_id"]')" "100"
ok "the new key gets this run's origin" "$(stored "$WORK/out17" 'store["origins"]["deploy_target"]["run_id"]')" "4242"

echo "18. share-env captures a value straight out of the environment"
reset_share
export captured_tag='v1.2.3 with "quotes" and $DOLLAR and `backticks`'
STORE_OUT="$WORK/out18" SHARE_ENV="captured_tag"
run "$FIXTURES/nope.yml" ""
ok "exit status" "$STATUS" "0"
ok "captured verbatim" "$(get "$OUT_FILE" captured_tag)" 'v1.2.3 with "quotes" and $DOLLAR and `backticks`'
ok "stored verbatim" "$(stored "$WORK/out18" 'store["values"]["captured_tag"]')" \
  'v1.2.3 with "quotes" and $DOLLAR and `backticks`'
unset captured_tag

echo "19. share-env naming an unset variable fails instead of publishing nothing"
reset_share
SHARE_ENV="definitely_not_set_anywhere"
run "$FIXTURES/nope.yml" ""
ok "exit status" "$STATUS" "1"
contains "names the variable" "$LOG" "definitely_not_set_anywhere"

echo "20. the key rules apply to the share path too"
reset_share
SHARE=$'GITHUB_TOKEN: nope\n'
run "$FIXTURES/nope.yml" ""
ok "reserved: exit status" "$STATUS" "1"
contains "reserved: names the key" "$LOG" "GITHUB_TOKEN"
reset_share
SHARE=$'node-version: "20"\n'
run "$FIXTURES/nope.yml" ""
ok "invalid: exit status" "$STATUS" "1"
contains "invalid: names the key" "$LOG" "node-version"

echo "21. a store is not a trusted input"
# An artifact anyone with write access could have crafted must not be able to
# rewrite PATH for the rest of the job.
printf '%s' '{"schema":1,"scope":"main","values":{"PATH":"/tmp/evil"},"origins":{}}' >"$WORK/evil.json"
reset_share
LOAD_SHARED=true STORE_IN="$WORK/evil.json"
run "$FIXTURES/nope.yml" ""
ok "exit status" "$STATUS" "1"
contains "names the key" "$LOG" "PATH"
# The key is baked into an artifact, so unlike a bad key in an input there is
# nothing in the workflow to edit. Say so, or the error is a dead end.
contains "points at the remedy" "$LOG" "delete the store artifact"
# A producer hits the same wall: publishing reads the store it merges into, so
# it cannot publish its way past a poisoned one either.
reset_share
STORE_IN="$WORK/evil.json" STORE_OUT="$WORK/out21" SHARE=$'deploy_target: staging\n'
run "$FIXTURES/nope.yml" ""
ok "producer: exit status" "$STATUS" "1"
contains "producer: points at the remedy" "$LOG" "delete the store artifact"
# An unexportable name, not just a reserved one.
printf '%s' '{"schema":1,"scope":"main","values":{"node-version":"20"},"origins":{}}' >"$WORK/dashed.json"
reset_share
LOAD_SHARED=true STORE_IN="$WORK/dashed.json"
run "$FIXTURES/nope.yml" ""
ok "invalid name: exit status" "$STATUS" "1"
contains "invalid name: points at the remedy" "$LOG" "delete the store artifact"
# An input keeps the short message -- there is a workflow line to fix.
reset_share
SHARE=$'node-version: "20"\n'
run "$FIXTURES/nope.yml" ""
case "$LOG" in
  *"delete the store artifact"*) FAIL=$((FAIL+1)); echo "  FAIL an input error borrowed the store remedy";;
  *) PASS=$((PASS+1)); echo "  ok   an input error keeps the short message";;
esac

echo "22. an unreadable store is an error, not a silent fall-through"
printf '%s' 'not json at all' >"$WORK/garbage.json"
reset_share
LOAD_SHARED=true STORE_IN="$WORK/garbage.json"
run "$FIXTURES/nope.yml" ""
ok "malformed: exit status" "$STATUS" "1"
contains "malformed: explains" "$LOG" "not valid JSON"
printf '%s' '{"schema":99,"scope":"main","values":{},"origins":{}}' >"$WORK/future.json"
reset_share
LOAD_SHARED=true STORE_IN="$WORK/future.json"
run "$FIXTURES/nope.yml" ""
ok "future schema: exit status" "$STATUS" "1"
contains "future schema: explains" "$LOG" "schema"

echo "23. scopes map to artifact names without colliding"
ok "a clean scope stays readable" "$(name main)" "am-build-vars-store-main"
ok "a slugged scope is disambiguated" \
  "$([ "$(name 'feat/x')" = "$(name 'feat-x')" ] && echo collision || echo distinct)" "distinct"
ok "the same scope is stable" "$(name 'feat/x')" "$(name 'feat/x')"

echo "24. choose_artifact only trusts what it should"
# Newest wins, but the expired one, the one from another repository's run, the
# one with no run information and the one with no id are all skipped. Each of
# those four is newer than the winner, so a rule that stopped working would
# change this answer -- and the id-less one, being newest of all, would make
# choose() raise on the subscript rather than quietly return the wrong id.
ok "newest trusted artifact wins" "$(choose am-build-vars-store-main 999)" "5"
# Same fork-run artifact, now produced by the current run: a pull request from a
# fork has to be able to share values between its own jobs.
ok "the current run is always trusted" "$(choose am-build-vars-store-main 500)" "7"
ok "an unknown name finds nothing" "$(choose am-build-vars-store-nope 999)" ""
reset_share

echo "25. store.py's network path, against a local stand-in for the API"
# Everything above hands the resolver a store that is already on disk. This is the
# half that puts it there: the artifact listing, the redirect to signed storage,
# the zip, and the token that must not follow that redirect.
if HTTP_OUT="$(python3 "$ROOT/tests/store-http-test.py" 2>&1)"; then
  PASS=$((PASS + $(printf '%s' "$HTTP_OUT" | sed -n 's/^\([0-9]*\) passed.*/\1/p')))
  printf '  ok   listing, redirect, unzip, and the token that stays behind\n'
else
  FAIL=$((FAIL + 1))
  printf '  FAIL store.py network path:\n%s\n' "$(printf '%s' "$HTTP_OUT" | sed 's/^/       /')"
fi

echo "26. every spelling truthy() accepts loads the store"
# The composite's if-gate in action.yml gates the fetch on the same vocabulary.
# If the two ever drift, the step reads nothing and still exits 0 -- so pin the
# resolver half here, and the gate half in the workflow's share-consumer job.
for spelling in true True 1 yes on; do
  reset_share
  LOAD_SHARED="$spelling" STORE_IN="$FIXTURES/store-basic.json" STORE_RUN_ID=100
  run "$FIXTURES/nope.yml" ""
  ok "load-shared=$spelling applies the store" "$(get "$OUT_FILE" image_tag)" "sha-abc1234"
done
for spelling in false no 0 ""; do
  reset_share
  LOAD_SHARED="$spelling" STORE_IN="$FIXTURES/store-basic.json" STORE_RUN_ID=100
  run "$FIXTURES/nope.yml" ""
  ok "load-shared=${spelling:-<empty>} ignores the store" "$(get "$OUT_FILE" shared-json)" "{}"
done
reset_share

echo "27. a fetch that was skipped while sharing is on is an error, not a quiet zero"
# The composite's if-gate is a workflow expression and has no trim(), so it reads
# a padded 'load-shared' as false while truthy() reads it as true. Without this
# guard the step exits 0 having resolved nothing at all.
reset_share
LOAD_SHARED=true STORE_OUTCOME=skipped
run "$FIXTURES/nope.yml" ""
ok "load-shared: exit status" "$STATUS" "1"
contains "load-shared: names the inputs" "$LOG" "load-shared"
reset_share
SHARE=$'deploy_target: staging\n' STORE_OUTCOME=skipped STORE_OUT="$WORK/out27"
run "$FIXTURES/nope.yml" ""
# A producer here would publish a store built from nothing, erasing every key
# another workflow had put there.
ok "share: exit status" "$STATUS" "1"
reset_share
SHARE_ENV="captured_tag" STORE_OUTCOME=skipped
run "$FIXTURES/nope.yml" ""
ok "share-env: exit status" "$STATUS" "1"
# The ordinary no-sharing path reports the same 'skipped' and must stay silent.
reset_share
STORE_OUTCOME=skipped
run "$FIXTURES/basic.yml" ""
ok "sharing off: exit status" "$STATUS" "0"
ok "sharing off: still resolves" "$(get "$OUT_FILE" node_version)" "20"
# And a fetch that really ran is unaffected.
reset_share
LOAD_SHARED=true STORE_OUTCOME=success STORE_IN="$FIXTURES/store-basic.json" STORE_RUN_ID=100
run "$FIXTURES/nope.yml" ""
ok "fetch ran: exit status" "$STATUS" "0"
ok "fetch ran: store applied" "$(get "$OUT_FILE" image_tag)" "sha-abc1234"
reset_share

echo
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
