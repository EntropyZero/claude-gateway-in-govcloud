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

# ---- system_ca_bundle / combined_ca_bundle -------------------------------
# SSL_CERT_FILE is honored first, so the tests control the "system store"
# without depending on the host distro's CA layout.

@test "system_ca_bundle: honors SSL_CERT_FILE override" {
  printf 'SYSTEM-STORE\n' > "$BATS_TEST_TMPDIR/sys.pem"
  src "SSL_CERT_FILE='$BATS_TEST_TMPDIR/sys.pem' system_ca_bundle"
  [ "$status" -eq 0 ]
  [ "$output" = "$BATS_TEST_TMPDIR/sys.pem" ]
}

@test "combined_ca_bundle: system store comes first, extras appended in order" {
  printf 'SYSTEM-STORE' > "$BATS_TEST_TMPDIR/sys.pem"    # note: no trailing newline
  printf 'EXTRA-ONE' > "$BATS_TEST_TMPDIR/one.pem"
  printf 'EXTRA-TWO\n' > "$BATS_TEST_TMPDIR/two.pem"
  src "SSL_CERT_FILE='$BATS_TEST_TMPDIR/sys.pem' combined_ca_bundle '$BATS_TEST_TMPDIR/out.pem' '$BATS_TEST_TMPDIR/one.pem' '$BATS_TEST_TMPDIR/two.pem'"
  [ "$status" -eq 0 ]
  # newline-separated even when inputs lack trailing newlines (PEM markers
  # from adjacent files must never fuse into one line)
  run cat "$BATS_TEST_TMPDIR/out.pem"
  [ "${lines[0]}" = "SYSTEM-STORE" ]
  [ "${lines[1]}" = "EXTRA-ONE" ]
  [ "${lines[2]}" = "EXTRA-TWO" ]
}

@test "combined_ca_bundle: output file is mode 600" {
  printf 'S\n' > "$BATS_TEST_TMPDIR/sys.pem"
  printf 'E\n' > "$BATS_TEST_TMPDIR/e.pem"
  src "umask 022; SSL_CERT_FILE='$BATS_TEST_TMPDIR/sys.pem' combined_ca_bundle '$BATS_TEST_TMPDIR/out.pem' '$BATS_TEST_TMPDIR/e.pem'"
  [ "$status" -eq 0 ]
  [ "$(stat -c %a "$BATS_TEST_TMPDIR/out.pem")" = "600" ]
}

@test "combined_ca_bundle: unreadable extra CA fails closed" {
  printf 'S\n' > "$BATS_TEST_TMPDIR/sys.pem"
  src "SSL_CERT_FILE='$BATS_TEST_TMPDIR/sys.pem' combined_ca_bundle '$BATS_TEST_TMPDIR/out.pem' '$BATS_TEST_TMPDIR/missing.pem'"
  [ "$status" -eq 1 ]
}

# ---- require_mirrored_file / verify_sha256 (offline build hosts) ---------

@test "require_mirrored_file: passes silently when the artifact exists" {
  printf 'x' > "$BATS_TEST_TMPDIR/artifact.zip"
  src "require_mirrored_file '$BATS_TEST_TMPDIR/artifact.zip' 'scripts/mirror/foo.sh' && echo staged"
  [ "$status" -eq 0 ]
  [ "$output" = "staged" ]
}

@test "require_mirrored_file: missing artifact fails naming the mirror script and the transfer step" {
  src "require_mirrored_file '$BATS_TEST_TMPDIR/nope.zip' 'scripts/mirror/foo.sh'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"scripts/mirror/foo.sh"* ]]
  [[ "$output" == *"egress host"* ]]
  [[ "$output" == *"copy the mirror/"* ]]
}

@test "verify_sha256: matching digest passes" {
  printf 'payload' > "$BATS_TEST_TMPDIR/f"
  good="$(sha256sum "$BATS_TEST_TMPDIR/f" | cut -d' ' -f1)"
  src "verify_sha256 '$BATS_TEST_TMPDIR/f' '$good' && echo verified"
  [ "$status" -eq 0 ]
  [ "$output" = "verified" ]
}

@test "verify_sha256: mismatched digest fails closed with a re-stage hint" {
  printf 'payload' > "$BATS_TEST_TMPDIR/f"
  src "verify_sha256 '$BATS_TEST_TMPDIR/f' '0000000000000000000000000000000000000000000000000000000000000000'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"sha256 mismatch"* ]]
  [[ "$output" == *"egress host"* ]]
}

# ---- split_image_ref (base-image mirroring) -------------------------------

@test "split_image_ref: plain repo:tag" {
  src "split_image_ref 'grafana/grafana:13.1.1'"
  [ "$output" = "grafana/grafana 13.1.1" ]
}

@test "split_image_ref: registry path with tag" {
  src "split_image_ref 'public.ecr.aws/lambda/python:3.12'"
  [ "$output" = "public.ecr.aws/lambda/python 3.12" ]
}

@test "split_image_ref: bare name defaults to latest" {
  src "split_image_ref 'amazonlinux'"
  [ "$output" = "amazonlinux latest" ]
}

@test "split_image_ref: registry port without tag is not a tag" {
  src "split_image_ref 'localhost:5000/foo'"
  [ "$output" = "localhost:5000/foo latest" ]
}

@test "split_image_ref: registry port AND tag" {
  src "split_image_ref 'localhost:5000/foo:1.2'"
  [ "$output" = "localhost:5000/foo 1.2" ]
}

@test "split_image_ref: digest suffix is stripped, tag defaults" {
  src "split_image_ref 'python@sha256:0000000000000000000000000000000000000000000000000000000000000000'"
  [ "$output" = "python latest" ]
}

@test "split_image_ref: tag plus digest keeps the tag" {
  src "split_image_ref 'docker.io/library/python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000'"
  [ "$output" = "docker.io/library/python 3.12-slim" ]
}

# ---- stack_param / resolve_kms_param ---------------------------------------
# aws is stubbed via PATH: STUB_KMS_ERR simulates a describe-stacks failure
# (text on stderr, exit 254, like the real CLI); otherwise STUB_KMS_PARAM is
# printed the way `--output text` renders a parameter value (empty string =
# empty line = stack-managed key).

make_aws_stub() {
  mkdir -p "$BATS_TEST_TMPDIR/bin"
  cat >"$BATS_TEST_TMPDIR/bin/aws" <<'EOF'
#!/usr/bin/env bash
# STUB_KMS_WARN simulates the benign stderr the real CLI emits on SUCCESSFUL
# calls (urllib3/botocore deprecation warnings)
[ -n "${STUB_KMS_WARN:-}" ] && echo "$STUB_KMS_WARN" >&2
if [ -n "${STUB_KMS_ERR:-}" ]; then echo "$STUB_KMS_ERR" >&2; exit 254; fi
printf '%s\n' "${STUB_KMS_PARAM:-}"
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/aws"
}

# run an expression with the aws stub first in PATH. Isolation matters: the
# invoking shell may export ALLOW_KMS_PARAM_CHANGE / KMS_KEY_ARN (an operator
# running make test mid-procedure) - unset them so each test builds its own
# environment; DEPLOY_ENV_FILE is pinned to a nonexistent path so a real
# deploy.env is never sourced.
stub() { run bash -c "export PATH='$BATS_TEST_TMPDIR/bin':\$PATH AWS_REGION=r DEPLOY_ENV_FILE='$BATS_TEST_TMPDIR/no-such-deploy.env'; COMMON_SH_OPTIONAL_ENV=1 source '$COMMON'; unset ALLOW_KMS_PARAM_CHANGE KMS_KEY_ARN STUB_KMS_PARAM STUB_KMS_ERR STUB_KMS_WARN; $1"; }

@test "stack_param: prints the deployed parameter value" {
  make_aws_stub
  stub "export STUB_KMS_PARAM='arn:aws-us-gov:kms:r:1:key/k'; stack_param db KmsKeyArn"
  [ "$status" -eq 0 ]
  [ "$output" = "arn:aws-us-gov:kms:r:1:key/k" ]
}

@test "resolve_kms_param: first deploy (no stack) uses deploy.env KMS_KEY_ARN" {
  make_aws_stub
  stub "export STUB_KMS_ERR='An error occurred (ValidationError): Stack with id db does not exist'
        export KMS_KEY_ARN='arn:byo'; resolve_kms_param db 2>/dev/null"
  [ "$status" -eq 0 ]
  [ "$output" = "arn:byo" ]
}

@test "resolve_kms_param: existing stack-managed key is preserved (persisted output must not flip ownership)" {
  make_aws_stub
  stub "export STUB_KMS_PARAM=''; export KMS_KEY_ARN='arn:persisted-output'
        resolve_kms_param db 2>/dev/null"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "resolve_kms_param: warns when preserving a stack value that differs from deploy.env" {
  make_aws_stub
  stub "export STUB_KMS_PARAM=''; export KMS_KEY_ARN='arn:persisted-output'
        resolve_kms_param db 2>&1 >/dev/null"
  [ "$status" -eq 0 ]
  [[ "$output" == *"is NOT passed"* ]]
}

@test "resolve_kms_param: matching stack and deploy.env values pass through silently" {
  make_aws_stub
  stub "export STUB_KMS_PARAM='arn:same'; export KMS_KEY_ARN='arn:same'; resolve_kms_param db"
  [ "$status" -eq 0 ]
  [ "$output" = "arn:same" ]
}

@test "resolve_kms_param: ALLOW_KMS_PARAM_CHANGE=1 is the named override" {
  make_aws_stub
  stub "export STUB_KMS_PARAM='arn:old'; export KMS_KEY_ARN='arn:new'
        export ALLOW_KMS_PARAM_CHANGE=1; resolve_kms_param db 2>/dev/null"
  [ "$status" -eq 0 ]
  [ "$output" = "arn:new" ]
}

@test "resolve_kms_param: unexpected describe failure is fatal, not a fallback" {
  make_aws_stub
  stub "export STUB_KMS_ERR='An error occurred (AccessDenied) when calling DescribeStacks'
        export KMS_KEY_ARN='arn:byo'; resolve_kms_param db"
  [ "$status" -ne 0 ]
  [[ "$output" == *"refusing to guess key ownership"* ]]
}

@test "resolve_kms_param: benign CLI stderr warnings do not pollute the value (success path)" {
  make_aws_stub
  stub "export STUB_KMS_WARN='NotOpenSSLWarning: urllib3 v2 only supports OpenSSL'
        export STUB_KMS_PARAM=''; resolve_kms_param db 2>/dev/null"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "resolve_kms_param: non-ARN garbage value (incl. 'None') is fatal, never deployed" {
  make_aws_stub
  stub "export STUB_KMS_PARAM='None'; resolve_kms_param db"
  [ "$status" -ne 0 ]
  [[ "$output" == *"refusing to guess key ownership"* ]]
}

@test "resolve_kms_param: teardown+redeploy with a stale persisted ARN warns about BYO mode" {
  make_aws_stub
  stub "export STUB_KMS_ERR='An error occurred (ValidationError): Stack with id db does not exist'
        export KMS_KEY_ARN='arn:stale-persisted'; resolve_kms_param db 2>&1"
  [ "$status" -eq 0 ]
  [[ "$output" == *"BRING-YOUR-OWN"* ]]
  [[ "$output" == *"arn:stale-persisted"* ]]
}

# ---- json_string_from_file (managed claudeMd push) ------------------------
# Encodes CLAUDE_MD_FILE as a single-line JSON string literal for the
# ManagedClaudeMd CFN parameter; the rendered `claudeMd: "..."` line must
# parse as a YAML double-quoted scalar, so the encoding has to be exact.

@test "json_string_from_file: round-trips quotes, backslashes, \${} and unicode" {
  printf '# Rules\n- no "secrets" on CLI\n- path C:\\temp\n- ${VAR} stays literal\n- em\xe2\x80\x94dash\n' \
    > "$BATS_TEST_TMPDIR/rules.md"
  src "json_string_from_file '$BATS_TEST_TMPDIR/rules.md' > '$BATS_TEST_TMPDIR/enc'"
  [ "$status" -eq 0 ]
  # decoding the encoded value must reproduce the file byte-for-byte
  run python3 -c '
import json,sys
enc = open(sys.argv[1]).read()
orig = open(sys.argv[2], encoding="utf-8").read()
assert json.loads(enc) == orig, "round-trip mismatch"
print("ok")' "$BATS_TEST_TMPDIR/enc" "$BATS_TEST_TMPDIR/rules.md"
  [ "$status" -eq 0 ]
  [ "$output" = "ok" ]
}

@test "json_string_from_file: output is one double-quoted line (the CFN AllowedPattern shape)" {
  printf 'line one\nline two\n' > "$BATS_TEST_TMPDIR/two.md"
  src "json_string_from_file '$BATS_TEST_TMPDIR/two.md'"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  [[ "$output" == '"'*'"' ]]
  [[ "$output" != *$'\n'* ]]
}

@test "json_string_from_file: output is pure ASCII (chars == bytes for the 4096 limit)" {
  # deploy-gateway.sh bounds ${#MANAGED_CLAUDE_MD} (a CHAR count) against
  # CloudFormation's 4096-BYTE parameter limit - equivalent only while the
  # encoder emits pure ASCII (json.dumps ensure_ascii=True, the default).
  # This pins that property so an ensure_ascii=False edit fails loudly.
  printf 'em\xe2\x80\x94dash and emoji \xf0\x9f\x98\x80\n' > "$BATS_TEST_TMPDIR/nonascii.md"
  src "json_string_from_file '$BATS_TEST_TMPDIR/nonascii.md'"
  [ "$status" -eq 0 ]
  printf '%s' "$output" | LC_ALL=C grep -qE '^[ -~]+$'
}

@test "json_string_from_file: encoded value parses as a YAML scalar equal to the file" {
  # Keep this fixture BMP-only: PyYAML does NOT recombine JSON
  # surrogate-pair escapes (😀 stays two lone surrogates), while
  # the gateway's JS YAML parser does - an emoji here would fail the TEST
  # against content the GATEWAY round-trips fine (probed 2026-07-30).
  printf 'a: not a mapping\n- "not a list"\n#not a comment\n' > "$BATS_TEST_TMPDIR/tricky.md"
  src "json_string_from_file '$BATS_TEST_TMPDIR/tricky.md' > '$BATS_TEST_TMPDIR/enc2'"
  [ "$status" -eq 0 ]
  run python3 -c '
import json, sys
try:
    import yaml
except ImportError:
    # pyyaml is guaranteed under make test / CI (requirements-test.txt);
    # print a visible marker rather than silently passing elsewhere
    print("yaml-skipped: pyyaml not installed"); sys.exit(0)
enc = open(sys.argv[1]).read().rstrip("\n")
doc = yaml.safe_load("claudeMd: " + enc)
assert doc["claudeMd"] == open(sys.argv[2], encoding="utf-8").read()
print("yaml-ok")' "$BATS_TEST_TMPDIR/enc2" "$BATS_TEST_TMPDIR/tricky.md"
  [ "$status" -eq 0 ]
  [[ "$output" == "yaml-ok" || "$output" == yaml-skipped:* ]]
}

@test "json_string_from_file: missing file fails closed" {
  src "json_string_from_file '$BATS_TEST_TMPDIR/absent.md'"
  [ "$status" -ne 0 ]
}
