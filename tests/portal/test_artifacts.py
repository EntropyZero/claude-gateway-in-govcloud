"""install.cmd + README generation, ZIP assembly, sha lookup, audit shape."""

import io
import json
import zipfile

import pytest

from conftest import FakeS3

SHA = "3f1c" + "0" * 60  # 64 hex chars
GW = "https://claude-gateway.example.com"


# ------------------------------------------------------------- install.cmd


def test_install_cmd_bakes_all_arguments(app):
    cmd = app.build_install_cmd(GW, SHA, "platform", "CC-1000",
                                disable_updates=True, bundle_extra_ca=False)
    assert '-BinaryPath "%HERE%claude.exe"' in cmd
    assert "-Sha256 %s" % SHA in cmd
    assert '-GatewayUrl "%s"' % GW in cmd
    assert '-Team "platform"' in cmd
    assert '-CostCenter "CC-1000"' in cmd
    assert "-DisableUpdates" in cmd
    assert "Install-ClaudeCode.ps1" in cmd
    # CRLF line endings for Windows.
    assert "\r\n" in cmd


def test_install_cmd_omits_disable_updates_when_false(app):
    cmd = app.build_install_cmd(GW, SHA, "data", "CC-2000",
                                disable_updates=False, bundle_extra_ca=False)
    assert "-DisableUpdates" not in cmd


def test_install_cmd_no_ca_arg_when_not_bundled(app):
    cmd = app.build_install_cmd(GW, SHA, "data", "CC-2000",
                                disable_updates=True, bundle_extra_ca=False)
    assert "-ExtraCaCertPath" not in cmd
    assert "extra-ca.pem" not in cmd


def test_install_cmd_wires_ca_to_stable_path_when_bundled(app):
    cmd = app.build_install_cmd(GW, SHA, "data", "CC-2000",
                                disable_updates=True, bundle_extra_ca=True)
    assert "-ExtraCaCertPath" in cmd
    # Copies the transient extracted PEM to a stable path next to the binary.
    assert "claude-extra-ca.pem" in cmd
    assert 'copy /Y "%HERE%extra-ca.pem"' in cmd


def test_readme_mentions_context(app):
    r = app.build_readme(GW, "2.1.207", SHA, "platform", "CC-1000", bundle_extra_ca=False)
    assert "2.1.207" in r and GW in r and "platform" in r and "CC-1000" in r and SHA in r
    assert "extra-ca.pem" not in r
    r2 = app.build_readme(GW, "2.1.207", SHA, "platform", "CC-1000", bundle_extra_ca=True)
    assert "extra-ca.pem" in r2


# ------------------------------------------------------------- ZIP assembly


def _members(zip_bytes):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    return {i.filename: i for i in zf.infolist()}, zf


def test_zip_has_expected_members_and_stored_exe(app):
    out = io.BytesIO()
    exe = b"MZ" + b"\x00" * 1000
    app.stream_zip(out, [exe], b"<installer>", "cmd-body", "readme-body")
    infos, zf = _members(out.getvalue())
    assert set(infos) == {"claude.exe", "Install-ClaudeCode.ps1", "install.cmd", "README.txt"}
    # claude.exe is STORED (already-compressed binary, streamed).
    assert infos["claude.exe"].compress_type == zipfile.ZIP_STORED
    assert zf.read("claude.exe") == exe
    assert zf.read("install.cmd") == b"cmd-body"
    assert zf.read("Install-ClaudeCode.ps1") == b"<installer>"


def test_zip_includes_extra_ca_when_provided(app):
    out = io.BytesIO()
    app.stream_zip(out, [b"exe"], b"inst", "cmd", "readme", extra_ca_bytes=b"---CERT---")
    infos, zf = _members(out.getvalue())
    assert "extra-ca.pem" in infos
    assert zf.read("extra-ca.pem") == b"---CERT---"


def test_zip_streams_multichunk_exe(app):
    out = io.BytesIO()
    chunks = [b"A" * 500, b"B" * 500, b"C" * 250]
    app.stream_zip(out, chunks, b"inst", "cmd", "readme")
    _, zf = _members(out.getvalue())
    assert zf.read("claude.exe") == b"".join(chunks)


class _Unseekable:
    """Write-only, no seek/tell - proves stream_zip works on an HTTP response
    stream (the real wfile is unseekable)."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, b):
        self.buf.extend(b)
        return len(b)

    def flush(self):
        pass

    def seekable(self):
        return False

    def seek(self, *a):
        raise OSError("unseekable")

    def tell(self):
        raise OSError("unseekable")


def test_zip_writes_to_unseekable_stream(app):
    out = _Unseekable()
    app.stream_zip(out, [b"MZ" + b"\x00" * 4096], b"inst", "cmd", "readme")
    _, zf = _members(bytes(out.buf))
    assert zf.testzip() is None  # all CRCs check out
    assert zf.read("claude.exe") == b"MZ" + b"\x00" * 4096


# ------------------------------------------------------------- chunked writer


def test_chunked_writer_frames_and_terminates(app):
    from conftest import dechunk

    out = _Unseekable()
    cw = app.ChunkedWriter(out)
    cw.write(b"hello")
    cw.write(b"")          # empty write must NOT emit the terminator early
    cw.write(b"world!!")
    cw.close()
    raw = bytes(out.buf)
    assert raw.endswith(b"0\r\n\r\n")
    assert dechunk(raw) == b"helloworld!!"


def test_chunked_writer_zip_roundtrips(app):
    from conftest import dechunk

    out = _Unseekable()
    cw = app.ChunkedWriter(out)
    app.stream_zip(cw, [b"MZ" + b"\x00" * 3000], b"inst", "cmd", "readme")
    cw.close()
    _, zf = _members(dechunk(bytes(out.buf)))
    assert zf.testzip() is None
    assert zf.read("claude.exe") == b"MZ" + b"\x00" * 3000


# ------------------------------------------------------------- sha lookup


def test_release_sha256_reads_manifest(app, config, monkeypatch):
    manifest = {"platforms": {"win32-x64": {"checksum": SHA}}}
    fake = FakeS3({"releases/2.1.207/manifest.json": json.dumps(manifest).encode()})
    monkeypatch.setattr(app, "s3", fake)
    assert app.release_sha256(config) == SHA


# ------------------------------------------------------------- audit record


def test_audit_record_success_shape(app):
    rec = app.build_audit_record("success", "dev@example.com", ["g1"], "platform",
                                 "CC-1000", "2.1.207", SHA, "10.0.0.5", "curl/8")
    assert rec["outcome"] == "success"
    assert rec["user_email"] == "dev@example.com"
    assert rec["user_groups"] == ["g1"]
    assert rec["team"] == "platform" and rec["cost_center"] == "CC-1000"
    assert rec["version"] == "2.1.207" and rec["exe_sha256"] == SHA
    assert rec["source_ip"] == "10.0.0.5" and rec["user_agent"] == "curl/8"
    assert rec["event"] == "portal_download"
    assert "reason" not in rec


def test_audit_record_denied_carries_reason(app):
    rec = app.build_audit_record("denied", "dev@example.com", [], None, None,
                                 "2.1.207", None, "10.0.0.5", "curl/8",
                                 reason="not in access group")
    assert rec["outcome"] == "denied"
    assert rec["reason"] == "not in access group"
    assert rec["exe_sha256"] is None
