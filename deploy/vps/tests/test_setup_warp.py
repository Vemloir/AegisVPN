import hashlib
import io

import paramiko
import pytest

from deploy.vps import setup_warp


def test_verified_download_accepts_only_matching_sha256():
    payload = b"verified wgcf artifact"
    expected = hashlib.sha256(payload).hexdigest()

    assert (
        setup_warp.download_verified(
            "https://example.invalid/wgcf",
            expected,
            opener=lambda _url, timeout: io.BytesIO(payload),
        )
        == payload
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        setup_warp.download_verified(
            "https://example.invalid/wgcf",
            "0" * 64,
            opener=lambda _url, timeout: io.BytesIO(payload),
        )


def test_warp_ssh_client_rejects_unknown_host_keys():
    client = setup_warp.build_ssh_client()

    assert isinstance(client._policy, paramiko.RejectPolicy)


def test_remote_command_failure_stops_warp_installation():
    class Channel:
        def recv_exit_status(self):
            return 23

    class Stream:
        channel = Channel()

        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

    class Client:
        def exec_command(self, command, timeout):
            assert command == "wgcf generate"
            assert timeout == 15
            return None, Stream(b"partial output"), Stream(b"registration failed")

    with pytest.raises(RuntimeError, match="registration failed"):
        setup_warp.run_remote(Client(), "wgcf generate", timeout=15)
