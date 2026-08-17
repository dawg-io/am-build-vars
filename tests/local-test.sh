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
run() {
  OUT_FILE="$WORK/output.$RANDOM"
  ENV_FILE="$WORK/env.$RANDOM"
  : >"$OUT_FILE"
  : >"$ENV_FILE"
  LOG="$(
    GITHUB_WORKSPACE="$ROOT" \
    GITHUB_OUTPUT="$OUT_FILE" \
    GITHUB_ENV="$ENV_FILE" \
    INPUT_CONFIG_FILE="$1" \
    INPUT_DEFAULTS="${2:-}" \
    INPUT_EXPORT_ENV="${3:-true}" \
    INPUT_FAIL_ON_MISSING="${4:-false}" \
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
contains "log explains" "$LOG" "no config file at"

echo "3. precedence: file wins, untouched defaults survive"
run "tests/fixtures/override.yml" $'node_version: "18"\nrunner: ubuntu-22.04\ncoverage_enabled: false\n'
ok "exit status" "$STATUS" "0"
ok "file overrides" "$(get "$OUT_FILE" node_version)" "24"
ok "default survives (runner)" "$(get "$OUT_FILE" runner)" "ubuntu-22.04"
ok "default survives (bool)" "$(get "$OUT_FILE" coverage_enabled)" "false"
ok "json shape" "$(get "$OUT_FILE" json)" '{"node_version":"24","runner":"ubuntu-22.04","coverage_enabled":"false"}'

echo "4. both .yml and .yaml present -> failure"
run "tests/fixtures/dual.yml" ""
ok "exit status" "$STATUS" "1"
contains "names both files" "$LOG" "dual.yaml"
contains "names both files" "$LOG" "dual.yml"

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

echo
printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
