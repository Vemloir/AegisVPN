import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"AEGISVPN-BACKUP\x01"
_NONCE_SIZE = 12
_DATA_KEY_SIZE = 32


class BackupConfigurationError(RuntimeError):
    """The offline recovery key is absent or unsuitable."""


def encrypt_backup(plaintext: bytes, public_key_pem: bytes) -> bytes:
    """Encrypt a backup for an offline RSA recovery key.

    A fresh AES-256-GCM key protects the payload and is wrapped with
    RSA-OAEP-SHA256. Only the public key is needed on the production VPS.
    """
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except (TypeError, ValueError) as exc:
        raise BackupConfigurationError("backup public key is not valid PEM") from exc
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
        raise BackupConfigurationError("backup public key must be RSA with at least 2048 bits")

    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(_NONCE_SIZE)
    wrapped_key = public_key.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, _MAGIC)
    return _MAGIC + struct.pack(">I", len(wrapped_key)) + wrapped_key + nonce + ciphertext


def decrypt_backup(envelope: bytes, private_key_pem: bytes, password: bytes | None = None) -> bytes:
    """Decrypt an Aegis backup with the offline private key."""
    if not envelope.startswith(_MAGIC):
        raise ValueError("not an Aegis encrypted backup")
    offset = len(_MAGIC)
    if len(envelope) < offset + 4:
        raise ValueError("truncated Aegis backup")
    wrapped_size = struct.unpack(">I", envelope[offset : offset + 4])[0]
    offset += 4
    if wrapped_size < 256 or len(envelope) < offset + wrapped_size + _NONCE_SIZE + 16:
        raise ValueError("invalid Aegis backup envelope")
    wrapped_key = envelope[offset : offset + wrapped_size]
    offset += wrapped_size
    nonce = envelope[offset : offset + _NONCE_SIZE]
    ciphertext = envelope[offset + _NONCE_SIZE :]

    private_key = serialization.load_pem_private_key(private_key_pem, password=password)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("backup private key must be RSA")
    data_key = private_key.decrypt(
        wrapped_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return AESGCM(data_key).decrypt(nonce, ciphertext, _MAGIC)
