#!/usr/bin/env bats
# Unit tests for the sourceable functions in client/install-claude-code.sh
# (build_user_env + merge_user_settings), sourced under the
# CLAUDE_INSTALLER_DOTSOURCE guard - the same pattern tests/powershell uses
# for Install-ClaudeCode.ps1's Build-UserEnv/Write-UserSettings.

setup() {
  INSTALLER="$BATS_TEST_DIRNAME/../../client/install-claude-code.sh"
  SETTINGS="$BATS_TEST_TMPDIR/settings.json"
}

# helper: source the installer's functions in a clean shell and run the
# given expression.
src() { run bash -c "CLAUDE_INSTALLER_DOTSOURCE=1 source '$INSTALLER'; $1"; }

# ---- build_user_env ------------------------------------------------------

@test "build_user_env: disable-updates emits both lockdown keys" {
  src 'build_user_env 1 "" "" ""'
  [ "$status" -eq 0 ]
  [[ "$output" == *"DISABLE_UPDATES=1"* ]]
  [[ "$output" == *"DISABLE_AUTOUPDATER=1"* ]]
}

@test "build_user_env: team+cost-center emit one OTEL_RESOURCE_ATTRIBUTES" {
  src 'build_user_env 0 "CC-1000" "platform" ""'
  [ "$status" -eq 0 ]
  [ "$output" = "OTEL_RESOURCE_ATTRIBUTES=cost_center=CC-1000,team=platform" ]
}

@test "build_user_env: team only - no leading comma" {
  src 'build_user_env 0 "" "platform" ""'
  [ "$output" = "OTEL_RESOURCE_ATTRIBUTES=team=platform" ]
}

@test "build_user_env: extra CA emits NODE_EXTRA_CA_CERTS" {
  src 'build_user_env 0 "" "" "/home/dev/.local/bin/claude-extra-ca.pem"'
  [ "$output" = "NODE_EXTRA_CA_CERTS=/home/dev/.local/bin/claude-extra-ca.pem" ]
}

@test "build_user_env: nothing requested emits nothing, exit 0" {
  src 'build_user_env 0 "" "" ""'
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

# ---- merge_user_settings -------------------------------------------------

@test "merge_user_settings: creates a fresh settings.json with the env block" {
  src "merge_user_settings '$SETTINGS' DISABLE_UPDATES=1 OTEL_RESOURCE_ATTRIBUTES=team=x"
  [ "$status" -eq 0 ]
  run python3 -c "import json;d=json.load(open('$SETTINGS'));print(d['env']['DISABLE_UPDATES'],d['env']['OTEL_RESOURCE_ATTRIBUTES'])"
  [ "$output" = "1 team=x" ]
}

@test "merge_user_settings: preserves unrelated top-level and env keys" {
  printf '%s' '{"model":"opus","env":{"FOO":"bar"}}' > "$SETTINGS"
  src "merge_user_settings '$SETTINGS' DISABLE_UPDATES=1"
  [ "$status" -eq 0 ]
  run python3 -c "import json;d=json.load(open('$SETTINGS'));print(d['model'],d['env']['FOO'],d['env']['DISABLE_UPDATES'])"
  [ "$output" = "opus bar 1" ]
}

@test "merge_user_settings: value containing '=' survives the split" {
  src "merge_user_settings '$SETTINGS' OTEL_RESOURCE_ATTRIBUTES=cost_center=CC-1000,team=x"
  run python3 -c "import json;print(json.load(open('$SETTINGS'))['env']['OTEL_RESOURCE_ATTRIBUTES'])"
  [ "$output" = "cost_center=CC-1000,team=x" ]
}

@test "merge_user_settings: refuses to clobber an unparseable settings.json" {
  printf '%s' '{not json' > "$SETTINGS"
  src "merge_user_settings '$SETTINGS' DISABLE_UPDATES=1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"not valid JSON"* ]]
  # The broken file is untouched.
  [ "$(cat "$SETTINGS")" = '{not json' ]
}

@test "merge_user_settings: refuses a non-object settings.json" {
  printf '%s' '[1,2]' > "$SETTINGS"
  src "merge_user_settings '$SETTINGS' DISABLE_UPDATES=1"
  [ "$status" -ne 0 ]
  [ "$(cat "$SETTINGS")" = '[1,2]' ]
}

# ---- whole-script safety -------------------------------------------------

@test "installer refuses an unknown argument" {
  run bash "$INSTALLER" --binary-path /dev/null --bogus
  [ "$status" -ne 0 ]
  [[ "$output" == *"unknown argument"* ]]
}

@test "installer aborts on SHA-256 mismatch before touching HOME" {
  bin="$BATS_TEST_TMPDIR/claude"; printf 'ELF' > "$bin"
  home="$BATS_TEST_TMPDIR/home"; mkdir -p "$home"
  run env HOME="$home" bash "$INSTALLER" --binary-path "$bin" \
    --sha256 "$(printf '0%.0s' $(seq 64))"
  [ "$status" -ne 0 ]
  [[ "$output" == *"SHA-256 mismatch"* ]]
  [ ! -e "$home/.local/bin/claude" ]
}

@test "installer rejects a comma in --team" {
  run bash "$INSTALLER" --binary-path /dev/null --team "a,b"
  [ "$status" -ne 0 ]
  [[ "$output" == *"must not contain spaces or commas"* ]]
}
