#!/usr/bin/env bash
# Offline installer for Claude Code on Linux (x64) - no calls to claude.ai or
# downloads.claude.ai, and NO root required. Takes an already-downloaded
# `claude` binary (mirrored from downloads.claude.ai/claude-code-releases/
# <version>/ and verified against the GPG-signed manifest), places it in
# ~/.local/bin, and makes sure that directory is on the PATH.
#
# The Linux twin of client/Install-ClaudeCode.ps1 - same rollout model:
#
#   - the binary installs to ~/.local/bin (user scope, chmod 0755),
#   - workstation configuration (telemetry attributes, update lockdown,
#     enterprise CA trust) is written as an `env` block in the USER settings
#     file ~/.claude/settings.json,
#   - gateway sign-in requires an admin-delivered managed policy
#     (forceLoginMethod:"gateway" + forceLoginGatewayUrl). Claude Code offers
#     the "Cloud gateway" login ONLY from a managed source - on Linux that is
#     the root-owned file /etc/claude-code/managed-settings.json - never from
#     user settings, so this installer does NOT write it. See
#     docs/operations/client-config.md (Part II) for delivery.
#
# Integrity model: there is no Authenticode on Linux; the chain is the
# GPG-verified release manifest -> the mirror's recorded SHA-256 -> this
# script's check of the LOCAL staged copy (--sha256). The portal bakes the
# manifest value into the generated install.sh wrapper.
#
# Usage:
#   bash install-claude-code.sh --binary-path ./claude --sha256 <64-hex> \
#     --gateway-url https://claude-gateway.example.com \
#     [--team <team>] [--cost-center <cc>] [--disable-updates] \
#     [--extra-ca-cert-path /path/to/enterprise-ca.pem]
#
# Requires: bash, sha256sum (coreutils), python3 (for the settings.json merge
# - the merge is skipped with a warning when python3 is absent; the install
# itself still completes).
set -euo pipefail

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

# Merge KEY=VALUE pairs into the `env` block of a settings.json, preserving
# every existing top-level key and every unrelated env key. Refuses to
# overwrite a file it cannot parse (the user's file, possibly hand-edited -
# never clobber it). Pure-ish and sourceable, so tests exercise it directly
# (tests/bash/install-linux.bats).
merge_user_settings() {
  local settings_path="$1"; shift
  python3 - "$settings_path" "$@" <<'PYEOF'
import json, os, sys

path = sys.argv[1]
pairs = [a.split("=", 1) for a in sys.argv[2:]]
merged = {}
if os.path.isfile(path):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    if raw.strip():
        try:
            merged = json.loads(raw)
        except ValueError as exc:
            sys.stderr.write(
                "existing settings.json is not valid JSON - fix or remove it, "
                "then re-run (%s)\n" % exc)
            sys.exit(3)
        if not isinstance(merged, dict):
            sys.stderr.write("existing settings.json is not a JSON object\n")
            sys.exit(3)
env = merged.get("env")
env = dict(env) if isinstance(env, dict) else {}
for k, v in pairs:
    env[k] = v
merged["env"] = env
d = os.path.dirname(path)
if d:
    os.makedirs(d, exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(merged, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PYEOF
}

# Assemble the KEY=VALUE env pairs to merge (update lockdown + telemetry
# attributes + enterprise CA trust), one per line on stdout. Mirrors the
# PowerShell installer's Build-UserEnv. These are ordinary env vars honored
# from the user settings file - NOT the managed-only login keys.
build_user_env() {
  local disable_updates="$1" cost_center="$2" team="$3" extra_ca="$4"
  if [ "$disable_updates" = "1" ]; then
    # DISABLE_UPDATES blocks ALL update paths (background + manual
    # 'claude update' / 'claude install') - required for self-distributed
    # pinned versions. DISABLE_AUTOUPDATER is defense in depth.
    echo "DISABLE_UPDATES=1"
    echo "DISABLE_AUTOUPDATER=1"
  fi
  if [ -n "$cost_center" ] || [ -n "$team" ]; then
    local attrs=""
    [ -n "$cost_center" ] && attrs="cost_center=${cost_center}"
    if [ -n "$team" ]; then
      [ -n "$attrs" ] && attrs="${attrs},"
      attrs="${attrs}team=${team}"
    fi
    echo "OTEL_RESOURCE_ATTRIBUTES=${attrs}"
  fi
  [ -n "$extra_ca" ] && echo "NODE_EXTRA_CA_CERTS=${extra_ca}"
  return 0
}

# Tests source this file for the functions above without running the
# installer body (same guard as the PowerShell installer).
if [ -n "${CLAUDE_INSTALLER_DOTSOURCE:-}" ]; then
  return 0 2>/dev/null || exit 0
fi

# --- 0. Arguments + preconditions -------------------------------------------

BINARY_PATH="" SHA256="" GATEWAY_URL="" TEAM="" COST_CENTER="" EXTRA_CA=""
DISABLE_UPDATES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --binary-path)        BINARY_PATH="${2:?--binary-path needs a value}"; shift 2 ;;
    --sha256)             SHA256="${2:?--sha256 needs a value}"; shift 2 ;;
    --gateway-url)        GATEWAY_URL="${2:?--gateway-url needs a value}"; shift 2 ;;
    --team)               TEAM="${2:?--team needs a value}"; shift 2 ;;
    --cost-center)        COST_CENTER="${2:?--cost-center needs a value}"; shift 2 ;;
    --extra-ca-cert-path) EXTRA_CA="${2:?--extra-ca-cert-path needs a value}"; shift 2 ;;
    --disable-updates)    DISABLE_UPDATES=1; shift ;;
    -h|--help)            grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

# A root run would install into /root and write root's settings - developers
# would never see the binary (the analog of the PS1 installer's SYSTEM
# refusal). Managed policy is delivered separately, with root, by IT.
[ "$(id -u)" -ne 0 ] || \
  die "running as root. Run as the developer (no root needed); the managed login policy is delivered separately - see docs/operations/client-config.md."

# Comma/whitespace would break OTEL_RESOURCE_ATTRIBUTES parsing (same rule as
# the PowerShell installer's ValidatePattern). Argument-shape checks first,
# filesystem checks second.
case "$TEAM$COST_CENTER" in
  *[[:space:]]* | *,*) die "--team/--cost-center must not contain spaces or commas." ;;
esac
[ -n "$BINARY_PATH" ] || die "--binary-path is required."
[ -f "$BINARY_PATH" ] || die "binary not found: $BINARY_PATH"
[ -z "$EXTRA_CA" ] || [ -f "$EXTRA_CA" ] || die "--extra-ca-cert-path not found: $EXTRA_CA"
command -v sha256sum >/dev/null || die "sha256sum (coreutils) is required."

# --- 1. Stage locally, then verify the LOCAL copy ---------------------------
# Verifying at $BINARY_PATH (possibly a network share) and copying afterwards
# is a time-of-check/time-of-use hole. Everything below operates on this
# staged copy.
STAGED="$(mktemp "${TMPDIR:-/tmp}/claude-install-XXXXXX")"
trap 'rm -f "$STAGED"' EXIT
say "Staging ${BINARY_PATH} locally"
cp "$BINARY_PATH" "$STAGED"

if [ -n "$SHA256" ]; then
  say "Verifying SHA-256 against manifest value"
  ACTUAL="$(sha256sum "$STAGED" | awk '{print $1}')"
  EXPECTED="$(printf '%s' "$SHA256" | tr '[:upper:]' '[:lower:]')"
  [ "$ACTUAL" = "$EXPECTED" ] || \
    die "SHA-256 mismatch. expected=${EXPECTED} actual=${ACTUAL} - refusing to install."
  echo "    checksum OK (${ACTUAL})"
else
  warn "no --sha256 given - installing UNVERIFIED (pass the manifest checksum)."
fi

# --- 2. Install to ~/.local/bin ---------------------------------------------
# Same location the native installer manages, so a future move to the online
# installer or auto-updates needs no path changes.
INSTALL_DIR="${HOME}/.local/bin"
TARGET="${INSTALL_DIR}/claude"
say "Installing to ${TARGET}"
mkdir -p "$INSTALL_DIR"
# Unlike Windows, replacing a running binary is safe on Linux (rename()
# swaps the directory entry; the running process keeps its old inode) - so a
# running claude is a warning, not a refusal. Scoped to THIS user: other
# users' processes are irrelevant and pgrep matches all users by default.
if [ -e "$TARGET" ] && pgrep -x -u "$(id -u)" claude >/dev/null 2>&1; then
  warn "claude appears to be running - the binary is replaced on disk, but running sessions keep the old version until restarted."
fi
# Move the verified staging copy into place; never re-reads the source
# post-verify. mv across filesystems copies, so fix the mode explicitly.
mv -f "$STAGED" "$TARGET"
trap - EXIT
chmod 0755 "$TARGET"

# --- 3. Make sure ~/.local/bin is on the PATH -------------------------------
say "Ensuring ${INSTALL_DIR} is on the PATH"
case ":$PATH:" in
  *":${INSTALL_DIR}:"*) echo "    already present" ;;
  *)
    # Most distros put ~/.local/bin on the PATH via ~/.profile already; when
    # this shell doesn't have it, append a guarded line to ~/.bashrc (the
    # portal fleet's default shell). Other-shell users: add it yourself.
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    if [ -f "${HOME}/.bashrc" ] && grep -qF "$PATH_LINE" "${HOME}/.bashrc"; then
      echo "    already in ~/.bashrc (new terminals pick it up)"
    else
      printf '\n# Added by install-claude-code.sh\n%s\n' "$PATH_LINE" >> "${HOME}/.bashrc"
      echo "    added to ~/.bashrc (new terminals pick it up; non-bash shells: add it to your shell's profile)"
    fi
    ;;
esac

# --- 4. User-scope configuration (env block in settings.json) ---------------
# Ordinary env vars only - honored from the user's own settings file, no
# root, no policy keys. Enforcement (forced gateway login, version floor) is
# deliberately NOT attempted here; it belongs to the root-delivered
# /etc/claude-code/managed-settings.json (docs/operations/client-config.md)
# and to the gateway's own /managed/settings push.
USER_ENV="$(build_user_env "$DISABLE_UPDATES" "$COST_CENTER" "$TEAM" "$EXTRA_CA")"
if [ -n "$USER_ENV" ]; then
  SETTINGS="${HOME}/.claude/settings.json"
  say "Writing user configuration (${SETTINGS} env block)"
  if ! command -v python3 >/dev/null; then
    # Non-fatal: the binary is already installed and the gateway pushes
    # central config after login anyway.
    warn "python3 not found - user settings were NOT updated. Install python3 and re-run, or add these to the env block of ${SETTINGS} yourself:"
    printf '%s\n' "$USER_ENV" | sed 's/^/    /' >&2
  else
    PAIRS=()
    while IFS= read -r line; do PAIRS+=("$line"); done <<< "$USER_ENV"
    if merge_user_settings "$SETTINGS" ${PAIRS[@]+"${PAIRS[@]}"}; then
      echo "    user settings updated: ${SETTINGS}"
    else
      warn "user settings were NOT updated (${SETTINGS}) - see the message above; the install itself succeeded."
    fi
  fi
fi

# --- 5. Smoke test + sign-in instructions -----------------------------------
say "Verifying installation"
VERSION_OUT="$("$TARGET" --version)" || die "claude --version failed - the binary does not run on this machine."
echo "    claude --version -> ${VERSION_OUT}"
echo
echo "Done. Binary installed (user scope, no root)."
echo
echo "Gateway sign-in needs an admin-delivered policy setting."
echo "  Claude Code only offers the \"Cloud gateway\" login when forceLoginMethod"
echo "  and forceLoginGatewayUrl are set in a MANAGED source - on Linux the"
echo "  root-owned file /etc/claude-code/managed-settings.json - by design it"
echo "  will NOT appear in /login otherwise. Your IT team delivers this; see"
echo "  docs/operations/client-config.md."
if [ -n "$GATEWAY_URL" ]; then
  echo
  echo "If you have sudo on this machine, set it yourself once:"
  echo "  sudo mkdir -p /etc/claude-code"
  echo "  printf '%s\\n' '{\"forceLoginMethod\":\"gateway\",\"forceLoginGatewayUrl\":\"${GATEWAY_URL}\",\"forceRemoteSettingsRefresh\":true}' | sudo tee /etc/claude-code/managed-settings.json >/dev/null"
fi
echo
echo "Once the policy is present: open a NEW terminal and run  claude  - it"
echo "opens the pre-filled gateway login (no menu, no URL to type; press Enter"
echo "to connect), then complete the Okta sign-in in your browser. Confirm the"
echo "TLS fingerprint your IT team published at the first-connect prompt."
