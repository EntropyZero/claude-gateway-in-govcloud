#!/usr/bin/env bats
# Tests for the `csr` subcommand of import-enterprise-cert.sh - key-type
# selection (EC default / RSA opt-in), key-usage correctness per algorithm,
# and the 0600 key-file permission. Runs entirely locally (openssl only; no
# AWS), in a temp CWD because the script writes <FQDN>.key.pem / .csr there.

setup() {
  SCRIPT="$BATS_TEST_DIRNAME/../../scripts/import-enterprise-cert.sh"
  FQDN="cert-test.example.com"
  cd "$BATS_TEST_TMPDIR"
}

# openssl req -text over the generated CSR, for asserting algorithm/usage.
csr_text() { openssl req -in "${FQDN}.csr" -noout -text; }

@test "csr: default (no key type) is an EC P-256 key" {
  run bash "$SCRIPT" csr "$FQDN"
  [ "$status" -eq 0 ]
  [[ "$output" == *"EC P-256"* ]]
  run csr_text
  [[ "$output" == *"id-ecPublicKey"* ]]
  [[ "$output" == *"prime256v1"* ]]
}

@test "csr: EC key usage is digitalSignature only (no keyEncipherment)" {
  bash "$SCRIPT" csr "$FQDN"
  run csr_text
  [[ "$output" == *"Digital Signature"* ]]
  [[ "$output" != *"Key Encipherment"* ]]
}

@test "csr: rsa2048 produces a 2048-bit RSA key" {
  run bash "$SCRIPT" csr "$FQDN" rsa2048
  [ "$status" -eq 0 ]
  [[ "$output" == *"RSA 2048"* ]]
  run csr_text
  [[ "$output" == *"rsaEncryption"* ]]
  [[ "$output" == *"2048 bit"* ]]
}

@test "csr: RSA key usage adds keyEncipherment (RSA key transport)" {
  bash "$SCRIPT" csr "$FQDN" rsa2048
  run csr_text
  [[ "$output" == *"Digital Signature"* ]]
  [[ "$output" == *"Key Encipherment"* ]]
}

@test "csr: rsa3072 produces a 3072-bit RSA key" {
  run bash "$SCRIPT" csr "$FQDN" rsa3072
  [ "$status" -eq 0 ]
  run csr_text
  [[ "$output" == *"3072 bit"* ]]
}

@test "csr: SAN and serverAuth EKU are present regardless of key type" {
  bash "$SCRIPT" csr "$FQDN" rsa2048
  run csr_text
  [[ "$output" == *"DNS:${FQDN}"* ]]
  [[ "$output" == *"TLS Web Server Authentication"* ]]
}

@test "csr: the private key file is created mode 0600" {
  bash "$SCRIPT" csr "$FQDN"
  [ "$(stat -c '%a' "${FQDN}.key.pem")" = "600" ]
}

@test "csr: an unknown key type fails and names the valid choices" {
  run bash "$SCRIPT" csr "$FQDN" ed25519
  [ "$status" -ne 0 ]
  [[ "$output" == *"ec | rsa2048 | rsa3072"* ]]
  [ ! -f "${FQDN}.csr" ]
}
