import hashlib
import re
from pathlib import Path

import pytest

from deploy.vps.update import verify_runtime_pins


ROOT = Path(__file__).resolve().parents[3]


def test_all_runtime_inputs_are_immutable_and_locked():
    compose = (ROOT / "deploy/vps/docker-compose.yml").read_text()
    dockerfiles = [
        (ROOT / "agent/Dockerfile").read_text(),
        (ROOT / "bot/Dockerfile.deploy").read_text(),
        (ROOT / "support_bot/Dockerfile").read_text(),
    ]

    assert "latest" not in compose.lower()
    assert "releases/latest" not in dockerfiles[0]
    assert "sha256sum -c -" in dockerfiles[0]
    for dockerfile in dockerfiles:
        assert "uv.lock" in dockerfile
        assert "uv sync --frozen" in dockerfile
        assert "@sha256:" in dockerfile
    for image in ("hysteria", "caddy", "mtg"):
        service = re.search(
            rf"(?ms)^  {image}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
            compose,
        ).group("body")
        assert "@sha256:" in service


def test_runtime_manifest_matches_the_pinned_sources():
    values = verify_runtime_pins(ROOT / "deploy/vps/runtime-versions.env")

    assert values["XRAY_VERSION"] == "v26.3.27"
    assert values["XRAY_SHA256"] == "23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae"
    assert values["HYSTERIA_VERSION"] == "v2.10.0"


def test_runtime_verifier_accepts_matching_archive_and_rejects_mismatch(tmp_path):
    archive = tmp_path / "xray.zip"
    archive.write_bytes(b"known archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = tmp_path / "runtime.env"
    manifest.write_text(
        "\n".join(
            [
                "PYTHON_IMAGE=python:3.14.6-slim@sha256:" + "a" * 64,
                "UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:" + "b" * 64,
                "XRAY_VERSION=v26.3.27",
                f"XRAY_SHA256={digest}",
                "HYSTERIA_VERSION=v2.10.0",
                "HYSTERIA_IMAGE=tobyxdd/hysteria@sha256:" + "c" * 64,
                "CADDY_IMAGE=caddy:2.11.4@sha256:" + "d" * 64,
                "MTG_IMAGE=ghcr.io/9seconds/mtg:2@sha256:" + "e" * 64,
            ]
        )
        + "\n"
    )

    verify_runtime_pins(manifest, archive)
    archive.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_runtime_pins(manifest, archive)
