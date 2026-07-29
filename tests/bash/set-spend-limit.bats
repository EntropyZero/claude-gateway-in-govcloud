#!/usr/bin/env bats
# Unit tests for set-spend-limit.sh's sourceable helpers (the
# CLAUDE_SETSPEND_DOTSOURCE guard): the email->principal resolution parser
# and the cap-row-id lookup parser. The gateway matches user caps by EXACT
# principal (oidc:<sub>) only, and clearing must DELETE the row (a null
# amount is an UNLIMITED override) - these parsers are what make the script
# honor that contract.

setup() {
  SCRIPT="$BATS_TEST_DIRNAME/../../scripts/set-spend-limit.sh"
}

# helper: source the script's functions in a clean shell and run the given
# expression. COMMON_SH_OPTIONAL_ENV: the script sources common.sh, which
# otherwise refuses to load without a filled-in deploy.env.
src() { run bash -c "COMMON_SH_OPTIONAL_ENV=1 CLAUDE_SETSPEND_DOTSOURCE=1 source '$SCRIPT'; $1"; }

# ---- parse_user_resolution ----------------------------------------------

EFFECTIVE_TWO_USERS='{"data":[
 {"actor":{"user_id":"oidc:00uBob","email_address":"bob.alice@example.com"}},
 {"actor":{"user_id":"oidc:00uAlice","email_address":"Alice@Example.com"}}]}'

@test "resolution: exact case-insensitive email wins over substring hits" {
  src "echo '$EFFECTIVE_TWO_USERS' | parse_user_resolution alice@example.com"
  [ "$status" -eq 0 ]
  [ "$output" = "oidc:00uAlice" ]
}

@test "resolution: no match errors with sign-in hint" {
  src "echo '{\"data\":[]}' | parse_user_resolution ghost@example.com"
  [ "$status" -ne 0 ]
  [[ "$output" == *"signed in"* ]]
}

@test "resolution: ambiguous email errors and names the principals" {
  doc='{"data":[{"actor":{"user_id":"oidc:00uA","email_address":"d@example.com"}},
               {"actor":{"user_id":"oidc:00uB","email_address":"d@example.com"}}]}'
  src "echo '$doc' | parse_user_resolution d@example.com"
  [ "$status" -ne 0 ]
  [[ "$output" == *"oidc:00uA"* && "$output" == *"oidc:00uB"* ]]
}

@test "resolution: unparseable body (HTML error page) fails closed" {
  src "echo '<html>upstream error</html>' | parse_user_resolution a@example.com"
  [ "$status" -ne 0 ]
  [[ "$output" == *"unparseable"* ]]
}

# ---- parse_limit_row_id --------------------------------------------------

LISTING='{"data":[
 {"id":"spl_1","scope":{"type":"user","user_id":"oidc:00uA"},"amount":"5000","period":"monthly"},
 {"id":"spl_2","scope":{"type":"user","user_id":"oidc:00uA"},"amount":null,"period":"daily"},
 {"id":"spl_3","scope":{"type":"rbac_group","rbac_group_id":"devs"},"amount":"2500","period":"monthly"},
 {"id":"spl_4","scope":{"type":"organization"},"amount":"99999","period":"monthly"}]}'

@test "row lookup: matches scope+id+period exactly" {
  src "echo '$LISTING' | parse_limit_row_id user oidc:00uA daily"
  [ "$status" -eq 0 ]
  [ "$output" = "spl_2" ]
}

@test "row lookup: org rows match on empty scope id" {
  src "echo '$LISTING' | parse_limit_row_id organization '' monthly"
  [ "$status" -eq 0 ]
  [ "$output" = "spl_4" ]
}

@test "row lookup: no row -> error, nothing to remove" {
  src "echo '$LISTING' | parse_limit_row_id user oidc:00uZ monthly"
  [ "$status" -ne 0 ]
  [[ "$output" == *"nothing to remove"* ]]
}

@test "row lookup: entered id beats the resolved-principal alt" {
  # A legacy email-keyed row and the principal's own cap both exist: the
  # entered email must match ITS row, never shadow-delete the real cap.
  doc='{"data":[
   {"id":"spl_real","scope":{"type":"user","user_id":"oidc:00uA"},"period":"monthly"},
   {"id":"spl_dead","scope":{"type":"user","user_id":"a@example.com"},"period":"monthly"}]}'
  src "echo '$doc' | parse_limit_row_id user a@example.com monthly oidc:00uA"
  [ "$status" -eq 0 ]
  [ "$output" = "spl_dead" ]
}

@test "row lookup: falls back to the alt id when the entered id has no row" {
  src "echo '$LISTING' | parse_limit_row_id user a@example.com monthly oidc:00uA"
  [ "$status" -eq 0 ]
  [ "$output" = "spl_1" ]
}

@test "row lookup: refuses an id that is unsafe in a URL path" {
  doc='{"data":[{"id":"spl_1/../x","scope":{"type":"user","user_id":"oidc:00uA"},"period":"monthly"}]}'
  src "echo '$doc' | parse_limit_row_id user oidc:00uA monthly"
  [ "$status" -ne 0 ]
  [[ "$output" == *"unexpected row id"* ]]
}

# ---- spend_urlencode -----------------------------------------------------

@test "urlencode: reserved characters in an email survive as query value" {
  src "spend_urlencode 'a+b@ex ample.com'"
  [ "$status" -eq 0 ]
  [ "$output" = "a%2Bb%40ex%20ample.com" ]
}
