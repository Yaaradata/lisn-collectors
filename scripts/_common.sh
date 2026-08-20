#!/usr/bin/env bash

set -euo pipefail

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

TRACE_FILE="docs/trace/S1.md"
mkdir -p "$(dirname "$TRACE_FILE")"

ok() {
  printf '\033[0;32m[OK]\033[0m %s\n' "$*"
}

warn() {
  printf '\033[0;33m[WARN]\033[0m %s\n' "$*"
}

fail() {
  printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"
  exit 1
}

need() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    fail "Missing required command on PATH: ${cmd}"
  fi
}

verify() {
  local description="$1"
  local expected="$2"
  local actual="$3"
  local result

  if [[ "$expected" == "$actual" ]]; then
    result="PASS"
    ok "${description}: expected='${expected}' actual='${actual}'"
  else
    result="FAIL"
    warn "${description}: expected='${expected}' actual='${actual}'"
  fi

  printf '| %s | %s | %s | %s |\n' \
    "$result" "$description" "$expected" "$actual" >>"$TRACE_FILE"
}

mask() {
  sed 's/:[^:@]*@/:***@/'
}
