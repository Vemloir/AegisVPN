import base64
import hashlib

import pytest

from deploy.vps import add_server


class _FakeKey:
    def __init__(self, raw: bytes):
        self._raw = raw

    def asbytes(self) -> bytes:
        return self._raw

    def get_name(self) -> str:
        return "ssh-ed25519"


def _fingerprint(raw: bytes) -> str:
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


def test_pinned_host_key_policy_accepts_only_expected_sha256():
    key = _FakeKey(b"real server host key")
    policy = add_server.PinnedHostKeyPolicy(_fingerprint(key.asbytes()))

    policy.missing_host_key(None, "203.0.113.10", key)

    with pytest.raises(Exception, match="host key fingerprint mismatch"):
        add_server.PinnedHostKeyPolicy(_fingerprint(b"attacker key")).missing_host_key(
            None,
            "203.0.113.10",
            key,
        )


def test_provisioning_source_never_enables_root_or_accepts_password_argv():
    source = add_server.Path(add_server.__file__).read_text()

    assert "--main-password" not in source
    assert "--new-password" not in source
    assert "PermitRootLogin yes" not in source
    assert "PasswordAuthentication yes" not in source
    assert "AutoAddPolicy" not in source


def test_docker_bootstrap_uses_verified_pinned_apt_packages():
    script = add_server.DOCKER_BOOTSTRAP

    assert "get.docker.com" not in script
    assert "Signed-By: /etc/apt/keyrings/docker.asc" in script
    assert "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" in script
    assert "docker-ce=5:29.7.1-1~" in script
    assert "docker-compose-plugin=5.3.1-1~" in script
    assert "apt-mark hold" in script


def test_non_root_commands_use_sudo_without_exposing_password_in_command():
    class _Channel:
        def shutdown_write(self):
            pass

        def recv_exit_status(self):
            return 0

    class _Stream:
        def __init__(self, payload=b""):
            self.payload = payload
            self.written = ""
            self.channel = _Channel()

        def write(self, value):
            self.written += value

        def flush(self):
            pass

        def read(self):
            return self.payload

    class _Client:
        _aegis_username = "ubuntu"
        _aegis_sudo_password = "sudo-secret"

        def exec_command(self, command, timeout):
            self.command = command
            self.stdin = _Stream()
            return self.stdin, _Stream(b"ok"), _Stream()

    client = _Client()
    code, out, err = add_server.exec_command(client, "id", timeout=10)

    assert (code, out, err) == (0, "ok", "")
    assert client.command == "sudo -S -p '' -- sh -c 'id'"
    assert "sudo-secret" not in client.command
    assert client.stdin.written == "sudo-secret\n"
