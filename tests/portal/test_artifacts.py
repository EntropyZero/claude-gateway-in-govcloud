"""install.cmd + README generation, streamed ZIP assembly, S3 helpers, sha
lookup, audit record shape + AuditLogger stream naming."""

import io
import json
import os
import zipfile

import pytest

from portal.artifacts import (build_install_cmd, build_readme, read_s3_bytes,
                              release_sha256, s3_chunks, zip_stream)
from portal.audit import AuditLogger, build_audit_record
from portal.config import Config

from conftest import TEST_ENV, FakeS3

SHA = "3f1c" + "0" * 60  # 64 hex chars
GW = "https://claude-gateway.example.com"


# ------------------------------------------------------------- install.cmd


def test_install_cmd_bakes_all_arguments():
    cmd = build_install_cmd(GW, SHA, "platform", "CC-1000",
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


def test_install_cmd_omits_disable_updates_when_false():
    cmd = build_install_cmd(GW, SHA, "data", "CC-2000",
                            disable_updates=False, bundle_extra_ca=False)
    assert "-DisableUpdates" not in cmd


def test_install_cmd_no_ca_arg_when_not_bundled():
    cmd = build_install_cmd(GW, SHA, "data", "CC-2000",
                            disable_updates=True, bundle_extra_ca=False)
    assert "-ExtraCaCertPath" not in cmd
    assert "extra-ca.pem" not in cmd


def test_install_cmd_wires_ca_to_stable_path_when_bundled():
    cmd = build_install_cmd(GW, SHA, "data", "CC-2000",
                            disable_updates=True, bundle_extra_ca=True)
    assert "-ExtraCaCertPath" in cmd
    # Copies the transient extracted PEM to a stable path next to the binary.
    assert "claude-extra-ca.pem" in cmd
    assert 'copy /Y "%HERE%extra-ca.pem"' in cmd


def test_readme_mentions_context():
    r = build_readme(GW, "2.1.207", SHA, "platform", "CC-1000", bundle_extra_ca=False)
    assert "2.1.207" in r and GW in r and "platform" in r and "CC-1000" in r and SHA in r
    assert "extra-ca.pem" not in r
    r2 = build_readme(GW, "2.1.207", SHA, "platform", "CC-1000", bundle_extra_ca=True)
    assert "extra-ca.pem" in r2


# ------------------------------------------------------------- ZIP assembly
# zip_stream is a GENERATOR: the WSGI server (gunicorn) emits its output with
# chunked transfer-encoding, so truncation stays detectable. The tests join
# the yielded chunks; the mid-stream failure test iterates chunk by chunk.


def _zip_bytes(*args, **kwargs):
    return b"".join(zip_stream(*args, **kwargs))


def _members(zip_bytes):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    return {i.filename: i for i in zf.infolist()}, zf


def test_zip_has_expected_members_and_stored_exe():
    exe = b"MZ" + b"\x00" * 1000
    infos, zf = _members(_zip_bytes([exe], b"<installer>", "cmd-body", "readme-body"))
    assert set(infos) == {"claude.exe", "Install-ClaudeCode.ps1", "install.cmd", "README.txt"}
    # claude.exe is STORED (already-compressed binary, streamed).
    assert infos["claude.exe"].compress_type == zipfile.ZIP_STORED
    assert zf.read("claude.exe") == exe
    assert zf.read("install.cmd") == b"cmd-body"
    assert zf.read("Install-ClaudeCode.ps1") == b"<installer>"
    assert zf.testzip() is None


def test_zip_includes_extra_ca_when_provided():
    infos, zf = _members(_zip_bytes([b"exe"], b"inst", "cmd", "readme",
                                    extra_ca_bytes=b"---CERT---"))
    assert "extra-ca.pem" in infos
    assert zf.read("extra-ca.pem") == b"---CERT---"


def test_zip_streams_multichunk_exe():
    chunks = [b"A" * 500, b"B" * 500, b"C" * 250]
    _, zf = _members(_zip_bytes(chunks, b"inst", "cmd", "readme"))
    assert zf.read("claude.exe") == b"".join(chunks)
    assert zf.testzip() is None


def test_zip_yields_incrementally_not_all_at_end():
    """The exe must stream out as it is read - the generator yields DURING
    exe iteration, so the whole binary is never buffered."""
    chunks = [b"A" * 70000, b"B" * 70000]
    gen = zip_stream(iter(chunks), b"inst", "cmd", "readme")
    first = next(gen)
    assert len(first) > 0
    # After the first yield, only part of the stream has been emitted.
    rest = b"".join(gen)
    total = first + rest
    assert len(first) < len(total)
    _, zf = _members(total)
    assert zf.read("claude.exe") == b"".join(chunks)


def test_zip_mid_stream_failure_propagates_and_truncates():
    """An exe-chunk exception mid-iteration escapes the generator (the WSGI
    server then drops the connection without the 0-chunk terminator). The
    bytes already yielded must NOT parse as a complete ZIP - truncation is
    detectable."""
    def failing_chunks():
        yield b"A" * 70000
        raise OSError("S3 read failed mid-stream")

    gen = zip_stream(failing_chunks(), b"inst", "cmd", "readme")
    got = [next(gen)]
    with pytest.raises(OSError, match="mid-stream"):
        for chunk in gen:
            got.append(chunk)
    partial = b"".join(got)
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(partial))


# ------------------------------------------------------------- S3 helpers


def test_read_s3_bytes_and_chunks():
    s3 = FakeS3({"k": b"x" * 100})
    assert read_s3_bytes(s3, "b", "k") == b"x" * 100
    assert b"".join(s3_chunks(s3, "b", "k", chunk_size=7)) == b"x" * 100


def test_release_sha256_reads_manifest():
    manifest = {"platforms": {"win32-x64": {"checksum": SHA}}}
    s3 = FakeS3({"releases/2.1.207/manifest.json": json.dumps(manifest).encode()})
    config = Config(dict(TEST_ENV))
    assert release_sha256(s3, config) == SHA


# ------------------------------------------------------------- audit record


def test_audit_record_success_shape():
    rec = build_audit_record("success", "dev@example.com", ["g1"], "platform",
                            "CC-1000", "2.1.207", SHA, "10.0.0.5", "curl/8")
    assert rec["outcome"] == "success"
    assert rec["user_email"] == "dev@example.com"
    assert rec["user_groups"] == ["g1"]
    assert rec["team"] == "platform" and rec["cost_center"] == "CC-1000"
    assert rec["version"] == "2.1.207" and rec["exe_sha256"] == SHA
    assert rec["source_ip"] == "10.0.0.5" and rec["user_agent"] == "curl/8"
    assert rec["event"] == "portal_download"
    assert "reason" not in rec


def test_audit_record_denied_carries_reason():
    rec = build_audit_record("denied", "dev@example.com", [], None, None,
                            "2.1.207", None, "10.0.0.5", "curl/8",
                            reason="not in access group")
    assert rec["outcome"] == "denied"
    assert rec["reason"] == "not in access group"
    assert rec["exe_sha256"] is None


# ------------------------------------------------------------- AuditLogger


class FakeLogs:
    def __init__(self, fail=False):
        self.fail = fail
        self.streams = []
        self.events = []

    def create_log_stream(self, logGroupName, logStreamName):
        if self.fail:
            raise RuntimeError("logs unavailable")
        self.streams.append((logGroupName, logStreamName))

    def put_log_events(self, logGroupName, logStreamName, logEvents):
        if self.fail:
            raise RuntimeError("logs unavailable")
        self.events.extend(logEvents)


def test_audit_logger_stream_name_includes_pid():
    """gunicorn runs multiple workers sharing hostname AND boot second; the
    PID in the stream name keeps writers from clobbering each other."""
    logs = FakeLogs()
    logger = AuditLogger(logs, "/grp")
    assert "-%d-" % os.getpid() in logger.stream
    assert logs.streams == [("/grp", logger.stream)]


def test_audit_logger_writes_json_line():
    logs = FakeLogs()
    logger = AuditLogger(logs, "/grp")
    logger.write({"event": "portal_download", "outcome": "success"})
    assert len(logs.events) == 1
    assert json.loads(logs.events[0]["message"])["outcome"] == "success"


def test_audit_logger_failure_never_raises():
    # Audit failure must never abort a request path (log-and-continue) -
    # and a failing create_log_stream must not kill boot either.
    logger = AuditLogger(FakeLogs(fail=True), "/grp")
    logger.write({"event": "x"})  # must not raise
