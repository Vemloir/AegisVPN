# Encrypted disaster-recovery backups

Aegis backups contain the SQLite database, reconstructed `bot.env`, and the
main node's `agent.env`. They are encrypted with AES-256-GCM; the per-archive
data key is wrapped with an offline RSA-OAEP-SHA256 public key. Production has
only the public key, so compromising the VPS or a Telegram backup message does
not provide the decryption key.

## One-time offline key setup

Run this on an administrator workstation, not on the VPS:

```bash
cd bot
.venv/bin/python -m src.backup_cli generate-key \
  --private /secure/offline/aegis-backup-private.pem \
  --public /tmp/aegis-backup-public.pem
```

Store `aegis-backup-private.pem` offline in at least two protected locations.
Copy only the public file to the control server as:

```text
deploy/vps/data/control/server/backup-public-key.pem
```

The bot sees it read-only at `/control/backup-public-key.pem`. If the file is
absent or invalid, backup generation fails closed instead of writing plaintext.

## Restore

On the offline workstation:

```bash
cd bot
.venv/bin/python -m src.backup_cli decrypt \
  --private /secure/offline/aegis-backup-private.pem \
  /path/to/AegisVPN-BACKUP-DD.MM.YYYY-HH:MM.aegis \
  --output /tmp/AegisVPN-RESTORE.tar.gz
```

The restored tar contains secrets. Keep it on an encrypted disk, extract it
only for recovery, and remove the plaintext copy afterward.

## Legacy plaintext archives

Old `AegisVPN-BACKUP-*.tar.gz` files and previously sent Telegram documents are
not made safe by this update. Delete them after verifying the first encrypted
backup and rotate the bot token, agent tokens, and Reality private keys if a
plaintext copy may have been exposed.
