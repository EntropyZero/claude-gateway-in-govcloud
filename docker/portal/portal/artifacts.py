"""Download-package generation: install.cmd / install.sh, README, streamed
ZIP, S3 helpers.

The ZIP is produced by a GENERATOR (zip_stream) so the WSGI server emits it
with chunked transfer-encoding (no Content-Length): a truncated download (S3
read error mid-stream, task recycle, ALB cut) omits the terminating 0-length
chunk and the client DETECTS the failure - a close-delimited body would look
successfully complete. The platform binary (claude.exe / claude) is STORED
(already compressed) and streamed chunk-by-chunk so it is never held in
memory. Both platforms ship as a ZIP (one streaming path, one client story);
the Linux README says to run `bash install.sh`, so nothing depends on unzip
restoring the executable bit - though the entries carry Unix modes for
unzippers that do.
"""

import time
import zipfile

from .selection import PLATFORMS


def build_install_cmd(gateway_url, sha256, team, cost_center, disable_updates, bundle_extra_ca):
    """Generate the one-double-click install.cmd wrapper. Windows batch; the
    caller's dropdown selections and the deployment's baked settings become
    Install-ClaudeCode.ps1 arguments."""
    lines = [
        "@echo off",
        "setlocal",
        "rem Claude Code installer - options baked in by the download portal.",
        'set "HERE=%~dp0"',
    ]
    ca_arg = ""
    if bundle_extra_ca:
        # The bundled PEM must live at a STABLE path (the extracted folder is
        # transient); copy it next to the binary, then point the installer there.
        lines += [
            'set "CADEST=%USERPROFILE%\\.local\\bin\\claude-extra-ca.pem"',
            'if exist "%HERE%extra-ca.pem" (',
            '  if not exist "%USERPROFILE%\\.local\\bin" mkdir "%USERPROFILE%\\.local\\bin"',
            '  copy /Y "%HERE%extra-ca.pem" "%CADEST%" >nul',
            ")",
        ]
        ca_arg = ' -ExtraCaCertPath "%CADEST%"'
    args = [
        '-BinaryPath "%HERE%claude.exe"',
        "-Sha256 %s" % sha256,
        '-GatewayUrl "%s"' % gateway_url,
        '-Team "%s"' % team,
        '-CostCenter "%s"' % cost_center,
    ]
    if disable_updates:
        args.append("-DisableUpdates")
    cmd = (
        'powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%Install-ClaudeCode.ps1" '
        + " ".join(args)
        + ca_arg
    )
    lines += [
        "",
        cmd,
        "",
        "if %ERRORLEVEL% NEQ 0 echo Install failed with code %ERRORLEVEL%.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def build_install_sh(gateway_url, sha256, team, cost_center, disable_updates, bundle_extra_ca):
    """Generate the one-command install.sh wrapper - the Linux twin of
    build_install_cmd. The caller's dropdown selections and the deployment's
    baked settings become install-claude-code.sh arguments. LF endings."""
    lines = [
        "#!/usr/bin/env bash",
        "# Claude Code installer - options baked in by the download portal.",
        "set -euo pipefail",
        'HERE="$(cd "$(dirname "$0")" && pwd)"',
    ]
    args = [
        '--binary-path "$HERE/claude"',
        "--sha256 %s" % sha256,
        '--gateway-url "%s"' % gateway_url,
        '--team "%s"' % team,
        '--cost-center "%s"' % cost_center,
    ]
    if disable_updates:
        args.append("--disable-updates")
    if bundle_extra_ca:
        # The bundled PEM must live at a STABLE path (the extracted folder is
        # transient); copy it next to the binary, then point the installer
        # there - mirroring install.cmd's CADEST.
        lines += [
            'CADEST="$HOME/.local/bin/claude-extra-ca.pem"',
            'if [ -f "$HERE/extra-ca.pem" ]; then',
            '  mkdir -p "$HOME/.local/bin"',
            '  cp "$HERE/extra-ca.pem" "$CADEST"',
            "fi",
        ]
        args.append('--extra-ca-cert-path "$CADEST"')
    lines += [
        "",
        'exec bash "$HERE/install-claude-code.sh" ' + " ".join(args),
    ]
    return "\n".join(lines) + "\n"


def _build_readme_linux(gateway_url, version, sha256, team, cost_center, bundle_extra_ca):
    ca_note = (
        "  - extra-ca.pem          : your enterprise/TLS-inspection root CA;\n"
        "                            install.sh copies it beside the binary and\n"
        "                            trusts it.\n"
        if bundle_extra_ca
        else ""
    )
    return (
        "Claude Code - offline install package (Linux x64)\n"
        "=================================================\n\n"
        "Version:      %s\n"
        "Gateway:      %s\n"
        "Team:         %s\n"
        "Cost center:  %s\n"
        "claude SHA-256:\n  %s\n\n"
        "To install: unzip this package, then run   bash install.sh\n"
        "No root is needed: it installs the claude binary to ~/.local/bin,\n"
        "verifies the SHA-256 against the release manifest, and writes your\n"
        "telemetry tags and update lockdown into your user Claude settings.\n\n"
        "Package contents:\n"
        "  - claude                : the Claude Code binary (linux-x64, glibc -\n"
        "                            standard distributions; not Alpine/musl).\n"
        "  - install-claude-code.sh: the installer (unmodified).\n"
        "  - install.sh            : runs the installer with your options.\n"
        "%s"
        "\nSigning in to the gateway:\n"
        "  Gateway login needs a one-time policy setting from your IT team -\n"
        "  on Linux the root-owned file /etc/claude-code/managed-settings.json\n"
        "  - the 'Cloud gateway' login does not appear without it. (Gateway\n"
        "  URL, for reference: %s)\n"
        "  Once that policy is in place: open a NEW terminal and run  claude .\n"
        "  It opens the pre-filled gateway login (no menu, no URL to type; press\n"
        "  Enter to connect), then your browser for a one-time sign-in. Confirm\n"
        "  the gateway certificate fingerprint with IT at the first-connect prompt.\n"
        % (version, gateway_url, team, cost_center, sha256, ca_note, gateway_url)
    )


def build_readme(gateway_url, version, sha256, team, cost_center, bundle_extra_ca,
                 platform="windows"):
    if platform == "linux":
        return _build_readme_linux(gateway_url, version, sha256, team,
                                   cost_center, bundle_extra_ca)
    ca_note = (
        "  - extra-ca.pem      : your enterprise/TLS-inspection root CA; install.cmd\n"
        "                        copies it beside claude.exe and trusts it.\n"
        if bundle_extra_ca
        else ""
    )
    return (
        "Claude Code - offline install package\r\n"
        "=====================================\r\n\r\n"
        "Version:      %s\r\n"
        "Gateway:      %s\r\n"
        "Team:         %s\r\n"
        "Cost center:  %s\r\n"
        "claude.exe SHA-256:\r\n  %s\r\n\r\n"
        "To install: double-click install.cmd and follow the prompts.\r\n"
        "No administrator rights are needed: it installs claude.exe to\r\n"
        "%%USERPROFILE%%\\.local\\bin, verifies the SHA-256 and Anthropic's\r\n"
        "Authenticode signature, and writes your telemetry tags and update\r\n"
        "lockdown into your user Claude settings.\r\n\r\n"
        "Package contents:\r\n"
        "  - claude.exe            : the Claude Code binary (win32-x64).\r\n"
        "  - Install-ClaudeCode.ps1: the installer (unmodified).\r\n"
        "  - install.cmd           : runs the installer with your options.\r\n"
        "%s"
        "\r\nSigning in to the gateway:\r\n"
        "  Gateway login needs a one-time policy setting from your IT team,\r\n"
        "  delivered by group policy / MDM - the 'Cloud gateway' login does\r\n"
        "  not appear without it. (Gateway URL, for reference: %s)\r\n"
        "  Once that policy is in place: open a NEW terminal and run  claude .\r\n"
        "  It opens the pre-filled gateway login (no menu, no URL to type; press\r\n"
        "  Enter to connect), then your browser for a one-time sign-in. Confirm\r\n"
        "  the gateway certificate fingerprint with IT at the first-connect prompt.\r\n"
        % (version, gateway_url, team, cost_center, sha256, ca_note, gateway_url)
    )


class _DrainBuffer:
    """Unseekable write sink zipfile can write through; the generator drains
    accumulated bytes between writes. tell()/seek() raise so ZipFile takes its
    unseekable-stream path (data descriptors after each member), exactly like
    the old ChunkedWriter."""

    def __init__(self):
        self._chunks = []

    def write(self, data):
        n = len(data)
        if n:
            self._chunks.append(bytes(data))
        return n

    def drain(self):
        if not self._chunks:
            return b""
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out

    def flush(self):
        pass

    def seekable(self):
        return False

    def tell(self):
        raise OSError("streamed zip buffer is not seekable")

    def seek(self, *a):
        raise OSError("streamed zip buffer is not seekable")


def _entry_info(arcname, mode):
    # Real timestamp: a bare ZipInfo(arcname) defaults to 1980-01-01, which
    # extracted files would then show in Explorer/ls.
    info = zipfile.ZipInfo(arcname, date_time=time.localtime()[:6])
    # Unix creator, explicitly: create_system otherwise depends on the BUILD
    # platform, and without it unzippers ignore the Unix mode bits.
    info.create_system = 3
    info.external_attr = mode << 16
    return info


def zip_stream(exe_chunks, entries, binary_name="claude.exe", binary_mode=0o644):
    """Yield the download ZIP as a stream of byte chunks.

    The platform binary (`binary_name`) is STORED and streamed from
    `exe_chunks` (an iterable of bytes); ZipFile.open(...,'w') computes the
    CRC as it writes and emits a data descriptor, so it works on the
    unseekable buffer. `entries` is an ordered iterable of (arcname, data,
    unix_mode) written after it - data may be str (UTF-8) or bytes. An
    exception from `exe_chunks` mid-iteration propagates out of the
    generator: the WSGI server aborts the chunked response without the
    terminating 0-chunk, which is exactly the detectable-truncation
    contract."""
    buf = _DrainBuffer()
    with zipfile.ZipFile(buf, "w") as zf:
        info = _entry_info(binary_name, binary_mode)
        info.compress_type = zipfile.ZIP_STORED
        with zf.open(info, "w") as dest:
            for chunk in exe_chunks:
                if chunk:
                    dest.write(chunk)
                    data = buf.drain()
                    if data:
                        yield data
        for arcname, data, mode in entries:
            zf.writestr(_entry_info(arcname, mode), data)
    data = buf.drain()
    if data:
        yield data


# ---------------------------------------------------------------- S3 helpers
# The s3 client is passed explicitly (create_app injects it) - no module
# globals, so tests fake it per-app.


def read_s3_bytes(s3, bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def s3_chunks(s3, bucket, key, chunk_size=1024 * 1024):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        yield chunk


def release_sha256(s3, config, platform="windows"):
    """The platform binary's SHA-256 from the published manifest.json -
    reusing the verified mirror output, never trusting a value from the
    client."""
    import json
    key = "releases/%s/manifest.json" % config.release_version
    manifest = json.loads(read_s3_bytes(s3, config.artifacts_bucket, key))
    return manifest["platforms"][PLATFORMS[platform]["manifest_key"]]["checksum"]
