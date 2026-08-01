import io
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.core.config import settings
from src.scheduler import tasks
from src.services.backup_archive import BackupConfigurationError, decrypt_backup


def _write_keypair(tmp_path: Path) -> tuple[Path, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "backup-public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return public_path, private_pem


def test_full_backup_is_encrypted_before_persisting(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "aegis.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE secrets (value TEXT NOT NULL)")
        db.execute("INSERT INTO secrets VALUES ('sqlite marker and subscription secrets')")
    agent_env = tmp_path / "agent.env"
    agent_env.write_text("REALITY_PRIVATE_KEY=top-secret\nAGENT_TOKEN=node-secret\n")
    public_path, private_pem = _write_keypair(tmp_path)
    out_dir = tmp_path / "backups"

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "db_pass", None)
    monkeypatch.setattr(settings, "sqlite_path", str(db_path))
    monkeypatch.setattr(settings, "bootstrap_server_agent_env", str(agent_env))
    monkeypatch.setattr(settings, "backup_public_key_file", str(public_path))
    monkeypatch.setattr(settings, "backup_dir", str(out_dir))

    archive = tasks._make_full_backup()

    assert archive is not None
    assert archive.suffix == ".aegis"
    ciphertext = archive.read_bytes()
    assert b"top-secret" not in ciphertext
    assert b"node-secret" not in ciphertext
    assert settings.bot_token.get_secret_value().encode() not in ciphertext
    assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert not list(out_dir.glob("*.tar.gz"))
    assert not list(out_dir.glob("_*"))

    plaintext = decrypt_backup(ciphertext, private_pem)
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert names == {"aegis.db", "bot.env", "agent.env"}
        assert b"sqlite marker" in tar.extractfile("aegis.db").read()
        assert b"BOT_TOKEN=123:TEST" in tar.extractfile("bot.env").read()
        assert b"REALITY_PRIVATE_KEY=top-secret" in tar.extractfile("agent.env").read()


def test_backup_fails_closed_without_offline_public_key(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "aegis.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE secrets (value TEXT NOT NULL)")
        db.execute("INSERT INTO secrets VALUES ('database secret')")
    out_dir = tmp_path / "backups"

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "db_pass", None)
    monkeypatch.setattr(settings, "sqlite_path", str(db_path))
    monkeypatch.setattr(settings, "backup_public_key_file", str(tmp_path / "missing.pem"))
    monkeypatch.setattr(settings, "backup_dir", str(out_dir))

    with pytest.raises(BackupConfigurationError, match="public key"):
        tasks._make_full_backup()

    assert not out_dir.exists() or not any(out_dir.iterdir())
