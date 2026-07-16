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
