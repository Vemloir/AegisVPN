"""Offline key generation and recovery for encrypted Aegis backups."""

import argparse
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.services.backup_archive import decrypt_backup


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def generate_keypair(private_path: Path, public_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _write_private(private_path, private_pem)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_pem)
    public_path.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-key", help="create an offline RSA recovery keypair")
    generate.add_argument("--private", type=Path, required=True)
    generate.add_argument("--public", type=Path, required=True)
    decrypt = commands.add_parser("decrypt", help="decrypt an .aegis archive to .tar.gz")
    decrypt.add_argument("--private", type=Path, required=True)
    decrypt.add_argument("archive", type=Path)
    decrypt.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "generate-key":
        generate_keypair(args.private, args.public)
        return

    plaintext = decrypt_backup(args.archive.read_bytes(), args.private.read_bytes())
    _write_private(args.output, plaintext)


if __name__ == "__main__":
    main()
