# Upgrade Guide

Sovyx includes a **zero-downtime upgrade system** with automatic rollback.

## Quick Upgrade

```bash
# Check for available upgrades
sovyx doctor

# Run the upgrade
pip install --upgrade sovyx
sovyx upgrade
```

## How It Works

The upgrade pipeline follows a **blue-green** strategy:

```
backup → install → migrate → verify → swap → cleanup
```

1. **Backup** — Creates a full backup of your database before any changes
2. **Install** — Installs the new version
3. **Migrate** — Applies any pending schema migrations
4. **Verify** — Runs the Doctor diagnostic suite (11 health checks)
5. **Swap** — Atomically swaps to the new database
6. **Cleanup** — Removes temporary files and prunes old backups

If **any step fails**, Sovyx automatically rolls back to the pre-upgrade state.

## Doctor Diagnostics

The `sovyx doctor` command runs 11 health checks:

| Check | What it verifies |
|-------|-----------------|
| `db_integrity` | SQLite integrity check passes |
| `schema_version` | Schema version is valid and current |
| `brain_consistency` | Brain data is consistent |
| `config_valid` | Configuration is valid and loadable |
| `disk_space` | Sufficient disk space available |
| `memory_usage` | Memory usage within limits |
| `model_files` | Required model files exist |
| `port_available` | Dashboard port is available |
| `python_version` | Python version is compatible |
| `dependency_versions` | Dependencies are correct versions |
| `data_dir_writable` | Data directory is writable |

## Backup & Restore

### Automatic Backups

Sovyx creates backups automatically:

- **Before upgrades** — Always backs up before migration
- **Scheduled** — GFS retention (grandfather-father-son)
- **Manual** — On-demand via API

### Manual Backup

```python
from sovyx.upgrade import BackupManager, BackupTrigger

manager = BackupManager(pool, backup_dir)
info = await manager.create_backup(BackupTrigger.MANUAL)
print(f"Backup saved: {info.path}")
```

### Restore

```python
await manager.restore_backup(backup_path)
```

## Cloud Backup (Optional)

If configured, backups are encrypted with **AES-256-GCM** (Argon2id key derivation)
and uploaded to cloud storage. The encryption key never leaves your device.

```bash
# Enable in config
sovyx config set cloud.backup.enabled true
sovyx config set cloud.backup.passphrase "your-secure-passphrase"
```

!!! warning "Zero Knowledge"
    If you lose your passphrase, your cloud backups are **permanently unrecoverable**.
    This is by design — Sovyx never has access to your encryption key.
