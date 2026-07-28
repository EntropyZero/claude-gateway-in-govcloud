"""Download-package generation: install.cmd, README, streamed ZIP, S3 helpers.

The ZIP is produced by a GENERATOR (zip_stream) so the WSGI server emits it
with chunked transfer-encoding (no Content-Length): a truncated download (S3
read error mid-stream, task recycle, ALB cut) omits the terminating 0-length
chunk and the client DETECTS the failure - a close-delimited body would look
successfully complete. claude.exe is STORED (already compressed) and streamed
chunk-by-chunk so the whole binary is never held in memory.
"""

import zipfile


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


def build_readme(gateway_url, version, sha256, team, cost_center, bundle_extra_ca):
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


def zip_stream(exe_chunks, installer_bytes, install_cmd, readme, extra_ca_bytes=None):
    """Yield the download ZIP as a stream of byte chunks.

    claude.exe is STORED and streamed from `exe_chunks` (an iterable of
    bytes); ZipFile.open(...,'w') computes the CRC as it writes and emits a
    data descriptor, so it works on the unseekable buffer. An exception from
    `exe_chunks` mid-iteration propagates out of the generator: the WSGI
    server aborts the chunked response without the terminating 0-chunk, which
    is exactly the detectable-truncation contract."""
    buf = _DrainBuffer()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("claude.exe")
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16
        with zf.open(info, "w") as dest:
            for chunk in exe_chunks:
                if chunk:
                    dest.write(chunk)
                    data = buf.drain()
                    if data:
                        yield data
        zf.writestr("Install-ClaudeCode.ps1", installer_bytes)
        zf.writestr("install.cmd", install_cmd)
        zf.writestr("README.txt", readme)
        if extra_ca_bytes is not None:
            zf.writestr("extra-ca.pem", extra_ca_bytes)
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


def release_sha256(s3, config):
    """The win32-x64 SHA-256 from the published manifest.json - reusing the
    verified mirror output, never trusting a value from the client."""
    import json
    key = "releases/%s/manifest.json" % config.release_version
    manifest = json.loads(read_s3_bytes(s3, config.artifacts_bucket, key))
    return manifest["platforms"]["win32-x64"]["checksum"]
