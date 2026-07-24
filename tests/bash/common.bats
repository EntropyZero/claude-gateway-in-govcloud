#!/usr/bin/env bats
# Unit tests for the pure/file helpers in scripts/common.sh.
# Each helper runs in a fresh subshell that sources common.sh with
# COMMON_SH_OPTIONAL_ENV=1 (so it doesn't require a filled-in deploy.env).

setup() {
  COMMON="$BATS_TEST_DIRNAME/../../scripts/common.sh"
  ENVFILE="$BATS_TEST_TMPDIR/deploy.env"
}

# helper: source common.sh in a clean shell and run the given expression
src() { run bash -c "COMMON_SH_OPTIONAL_ENV=1 source '$COMMON'; $1"; }
srcf() { run bash -c "DEPLOY_ENV_FILE='$ENVFILE' COMMON_SH_OPTIONAL_ENV=1 source '$COMMON'; $1"; }

# ---- proxy_port ----------------------------------------------------------

@test "proxy_port: explicit port with userinfo credentials" {
  src 'proxy_port "http://user:pass@proxy.corp:8443"'
  [ "$status" -eq 0 ]
  [ "$output" = "8443" ]
}

@test "proxy_port: explicit port, no credentials" {
  src 'proxy_port "http://proxy.corp:3128"'
  [ "$output" = "3128" ]
}

@test "proxy_port: https with no port is suppressed (443 already covered)" {
  src 'proxy_port "https://proxy.corp"'
  [ "$output" = "" ]
}

@test "proxy_port: explicit 443 is suppressed" {
  src 'proxy_port "https://proxy.corp:443"'
  [ "$output" = "" ]
}

@test "proxy_port: empty input yields nothing" {
  src 'proxy_port ""'
  [ "$output" = "" ]
}

# ---- set_env_var ---------------------------------------------------------

@test "set_env_var: appends a new key" {
  printf 'export FOO="1"\n' > "$ENVFILE"
  srcf 'set_env_var BAR "hello"'
  [ "$status" -eq 0 ]
  grep -q '^export BAR="hello"$' "$ENVFILE"
  grep -q '^export FOO="1"$' "$ENVFILE"   # untouched
}

@test "set_env_var: replaces an existing key in place" {
  printf 'export FOO="old"\nexport BAR="keep"\n' > "$ENVFILE"
  srcf 'set_env_var FOO "new"'
  grep -q '^export FOO="new"$' "$ENVFILE"
  ! grep -q 'old' "$ENVFILE"
  [ "$(grep -c '^export FOO=' "$ENVFILE")" -eq 1 ]   # not duplicated
}

@test "set_env_var: preserves a trailing comment on replace" {
  printf 'export FOO="old"   # keep me\n' > "$ENVFILE"
  srcf 'set_env_var FOO "new"'
  grep -q '^export FOO="new"   # keep me$' "$ENVFILE"
}

@test "set_env_var: values with slashes need no escaping (ARNs/URLs)" {
  printf 'export FOO="x"\n' > "$ENVFILE"
  srcf 'set_env_var FOO "arn:aws-us-gov:kms:us-gov-west-1:1/key"'
  grep -q '^export FOO="arn:aws-us-gov:kms:us-gov-west-1:1/key"$' "$ENVFILE"
}

# ---- require_vars --------------------------------------------------------

@test "require_vars: fails and names an unset variable" {
  src 'unset NOPE; require_vars NOPE'
  [ "$status" -ne 0 ]
  [[ "$output" == *"NOPE"* ]]
}

@test "require_vars: passes when all set" {
  src 'SET_ME=1 require_vars SET_ME'
  [ "$status" -eq 0 ]
}

# ---- retry_n -------------------------------------------------------------

@test "retry_n: returns 0 immediately on first success" {
  src 'retry_n 3 0 true && echo done'
  [ "$status" -eq 0 ]
  [[ "$output" == *done* ]]
}

@test "retry_n: retries until the command succeeds" {
  # fails twice (no marker file), succeeds on attempt 3
  local marker="$BATS_TEST_TMPDIR/count"
  src "retry_n 5 0 bash -c 'n=\$(cat \"$marker\" 2>/dev/null || echo 0); n=\$((n+1)); echo \$n > \"$marker\"; [ \$n -ge 3 ]'"
  [ "$status" -eq 0 ]
  [ "$(cat "$marker")" = "3" ]
}

@test "retry_n: fails after exhausting attempts and runs exactly N times" {
  local marker="$BATS_TEST_TMPDIR/count2"
  src "retry_n 4 0 bash -c 'n=\$(cat \"$marker\" 2>/dev/null || echo 0); echo \$((n+1)) > \"$marker\"; false'"
  [ "$status" -ne 0 ]
  [ "$(cat "$marker")" = "4" ]
}

# ---- dollars_to_cents ----------------------------------------------------
# Money conversion for the gateway spend API (whole-number cents as a STRING).
# The float route (a*100+0.5 then %.0f) put "0.05" on 6 cents - these pin the
# exact-string behavior so that regression cannot come back.

@test "dollars_to_cents: whole dollars" {
  src 'dollars_to_cents 50'
  [ "$status" -eq 0 ]
  [ "$output" = "5000" ]
}

@test "dollars_to_cents: trailing .00 is the same as whole dollars" {
  src 'dollars_to_cents 50.00'
  [ "$status" -eq 0 ]
  [ "$output" = "5000" ]
}

@test "dollars_to_cents: sub-dollar amount does not double-round (0.05 -> 5)" {
  src 'dollars_to_cents 0.05'
  [ "$status" -eq 0 ]
  [ "$output" = "5" ]
}

@test "dollars_to_cents: single decimal place is padded, not truncated" {
  src 'dollars_to_cents 0.5'
  [ "$status" -eq 0 ]
  [ "$output" = "50" ]
}

@test "dollars_to_cents: cents are preserved exactly" {
  src 'dollars_to_cents 1234.56'
  [ "$status" -eq 0 ]
  [ "$output" = "123456" ]
}

@test "dollars_to_cents: large amount stays exact (no float precision loss)" {
  src 'dollars_to_cents 99999999.99'
  [ "$status" -eq 0 ]
  [ "$output" = "9999999999" ]
}

@test "dollars_to_cents: rejects more than 2 decimal places rather than rounding money" {
  src 'dollars_to_cents 0.001'
  [ "$status" -eq 2 ]
}

@test "dollars_to_cents: rejects non-numeric input" {
  src 'dollars_to_cents abc'
  [ "$status" -eq 2 ]
}

@test "dollars_to_cents: rejects multiple dots" {
  src 'dollars_to_cents 1.2.3'
  [ "$status" -eq 2 ]
}

@test "dollars_to_cents: rejects empty input" {
  src 'dollars_to_cents ""'
  [ "$status" -eq 2 ]
}
