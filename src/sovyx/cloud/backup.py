"""Backup service — brain.db → compress → encrypt → R2 upload (zero-knowledge).

Orchestrates the full backup lifecycle: snapshot the database via VACUUM INTO,
gzip compress, encrypt with user passphrase (Argon2id + AES-256-GCM via
BackupCrypto), then upload the encrypted blob to Cloudflare R2 using the S3-
compatible API.

Restore reverses the process: download → decrypt → decompress → integrity
check → replace brain.db.

Wire format stored on R2::

    [gzip([brain.db VACUUM snapshot])] → encrypt → upload as .enc.gz

References:
    - SPE-033 §2.2: BackupService API
    - IMPL-SUP-008: R2/S3 compatibility layer
    - V05-06: BackupCrypto (Argon2id + AES-256-GCM)
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sovyx.cloud.crypto import BackupCrypto
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# Compression level: 6 is the default — good ratio without heavy CPU
GZIP_LEVEL = 6


@dataclass(frozen=True, slots=True)
class BackupConfig:
    """Configuration for backup operations.

    Attributes:
        r2_endpoint_url: Cloudflare R2 S3-compatible endpoint.
        r2_access_key_id: R2 access key.
        r2_secret_access_key: R2 secret key.
        r2_bucket: R2 bucket name.
        user_id: Account identifier for R2 key prefix.
        mind_id: Mind identifier for R2 key prefix.
    """

    r2_endpoint_url: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    user_id: str
    mind_id: str = "default"


@dataclass(frozen=True, slots=True)
class BackupMetadata:
    """Metadata describing a completed backup.

    Attributes:
        backup_id: Unique backup identifier (UUID4).
        created_at: UTC timestamp of backup creation.
        size_bytes: Size of the encrypted blob in bytes.
        compressed_size_bytes: Size after gzip, before encryption.
        original_size_bytes: Size of the raw VACUUM snapshot.
        brain_version: Schema version from brain.db (if available).
        sovyx_version: Sovyx package version.
        checksum: SHA-256 hex digest of the encrypted blob.
        r2_key: Object key in R2 storage.
    """

    backup_id: str
    created_at: datetime
    size_bytes: int
    compressed_size_bytes: int
    original_size_bytes: int
    brain_version: str
    sovyx_version: str
    checksum: str
    r2_key: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Result of a backup restore operation.

    Attributes:
        backup_id: The backup that was restored.
        restored_at: UTC timestamp of restore.
        original_size_bytes: Size of the restored database.
        integrity_ok: Whether PRAGMA integrity_check passed.
    """

    backup_id: str
    restored_at: datetime
    original_size_bytes: int
    integrity_ok: bool


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Summary information about an available backup.

    Attributes:
        backup_id: Unique backup identifier.
        created_at: UTC timestamp.
        size_bytes: Encrypted blob size.
        r2_key: Object key in R2.
    """

    backup_id: str
    created_at: datetime
    size_bytes: int
    r2_key: str


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Result of a backup pruning operation.

    Attributes:
        deleted_count: Number of backups removed.
        deleted_keys: R2 keys that were removed.
        remaining_count: Number of backups remaining.
    """

    deleted_count: int
    deleted_keys: list[str] = field(default_factory=list)
    remaining_count: int = 0


@runtime_checkable
class R2Client(Protocol):
    """Protocol for S3-compatible R2 operations.

    Abstracts boto3 so the service can be tested without real cloud calls.
    """

    def upload_bytes(self, data: bytes, key: str, bucket: str) -> None:
        """Upload bytes to a bucket/key."""
        ...

    def download_bytes(self, key: str, bucket: str) -> bytes:
        """Download bytes from a bucket/key."""
        ...

    def list_objects(self, prefix: str, bucket: str) -> list[dict[str, Any]]:
        """List objects under a prefix. Each dict has 'Key', 'Size', 'LastModified'."""
        ...

    def delete_objects(self, keys: list[str], bucket: str) -> int:
        """Delete objects by key. Returns count of deleted objects."""
        ...


class Boto3R2Client:
    """R2 client backed by boto3 S3-compatible API.

    Wraps boto3 operations to implement the :class:`R2Client` protocol.
    """

    def __init__(self, config: BackupConfig) -> None:
        import boto3  # type: ignore[import-not-found]  # lazy — optional dep

        self._bucket = config.r2_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=config.r2_endpoint_url,
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name="auto",
        )

    def upload_bytes(self, data: bytes, key: str, bucket: str) -> None:
        """Upload bytes to R2."""
        self._client.put_object(Bucket=bucket, Key=key, Body=data)

    def download_bytes(self, key: str, bucket: str) -> bytes:
        """Download bytes from R2."""
        response = self._client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()  # type: ignore[no-any-return]

    def list_objects(self, prefix: str, bucket: str) -> list[dict[str, Any]]:
        """List objects under prefix."""
        result: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                result.append(
                    {
                        "Key": obj["Key"],
                        "Size": obj["Size"],
                        "LastModified": obj["LastModified"],
                    }
                )
        return result

    def delete_objects(self, keys: list[str], bucket: str) -> int:
        """Delete objects by key."""
        if not keys:
            return 0
        # S3 delete_objects accepts max 1000 keys per call
        deleted = 0
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            response = self._client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch]},
            )
            deleted += len(response.get("Deleted", []))
        return deleted


def _get_sovyx_version() -> str:
    """Get the installed Sovyx version string."""
    try:
        from importlib.metadata import version

        return version("sovyx")
    except Exception:  # noqa: BLE001
        return "unknown"


def _get_brain_version(db_path: Path) -> str:
    """Read schema version from brain.db user_version pragma."""
    if not db_path.exists():
        return "unknown"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA user_version")
            row = cursor.fetchone()
            return str(row[0]) if row else "0"
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return "unknown"


def _vacuum_into(source_db: Path, dest: Path) -> None:
    """Create an atomic snapshot of a SQLite database using VACUUM INTO.

    This produces a consistent, standalone copy without holding locks for the
    entire duration. The destination file must not already exist.

    Args:
        source_db: Path to the source database.
        dest: Path for the vacuumed copy (must not exist).

    Raises:
        FileNotFoundError: If *source_db* does not exist.
        sqlite3.OperationalError: If VACUUM INTO fails.
    """
    if not source_db.exists():
        msg = f"Source database not found: {source_db}"
        raise FileNotFoundError(msg)
    conn = sqlite3.connect(str(source_db))
    try:
        conn.execute(f"VACUUM INTO '{dest}'")  # noqa: S608
    finally:
        conn.close()


class BackupService:
    """Zero-knowledge backup orchestrator.

    Encrypts data client-side before upload — the R2 server never sees
    plaintext or the encryption key.

    Usage::

        service = BackupService(
            db_path=Path("~/.sovyx/default/brain.db"),
            r2_client=Boto3R2Client(config),
            password="user-passphrase",
            config=config,
        )
        metadata = service.create_backup()
        result = service.restore_backup(metadata.backup_id, Path("/tmp/restore"))
    """

    def __init__(
        self,
        db_path: Path,
        r2_client: R2Client,
        password: str,
        config: BackupConfig,
    ) -> None:
        if not password:
            msg = "Backup password must not be empty"
            raise ValueError(msg)

        self._db_path = db_path
        self._r2 = r2_client
        self._password = password
        self._config = config
        self._prefix = f"{config.user_id}/{config.mind_id}"

    def create_backup(self, *, tmp_dir: Path | None = None) -> BackupMetadata:
        """Create an encrypted backup and upload to R2.

        Steps:
            1. VACUUM INTO temporary file (atomic, consistent snapshot).
            2. gzip compress (typically 3:1 ratio on brain.db).
            3. AES-256-GCM encrypt with BackupCrypto.
            4. Upload to R2: ``{user_id}/{mind_id}/{timestamp}_{backup_id}.enc.gz``.
            5. Return BackupMetadata with sizes, checksum, and R2 key.

        Args:
            tmp_dir: Directory for temporary files. Uses system temp if ``None``.

        Returns:
            BackupMetadata describing the uploaded backup.

        Raises:
            FileNotFoundError: If the source database does not exist.
            sqlite3.OperationalError: If VACUUM INTO fails.
        """
        import tempfile

        backup_id = uuid.uuid4().hex
        now = datetime.now(tz=UTC)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")

        if tmp_dir is not None:
            work_dir = Path(tmp_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="sovyx-backup-"))

        try:
            # Step 1: VACUUM INTO
            snapshot_path = work_dir / "snapshot.db"
            _vacuum_into(self._db_path, snapshot_path)
            raw_data = snapshot_path.read_bytes()
            original_size = len(raw_data)

            logger.debug(
                "backup_snapshot_created",
                size_bytes=original_size,
                backup_id=backup_id,
            )

            # Step 2: gzip compress
            compressed = gzip.compress(raw_data, compresslevel=GZIP_LEVEL)
            compressed_size = len(compressed)

            logger.debug(
                "backup_compressed",
                original=original_size,
                compressed=compressed_size,
                ratio=f"{original_size / compressed_size:.1f}:1" if compressed_size else "N/A",
            )

            # Step 3: encrypt
            encrypted = BackupCrypto.encrypt(compressed, self._password)
            encrypted_size = len(encrypted)

            # Step 4: compute checksum
            checksum = hashlib.sha256(encrypted).hexdigest()

            # Step 5: upload
            r2_key = f"{self._prefix}/{timestamp}_{backup_id}.enc.gz"
            self._r2.upload_bytes(encrypted, r2_key, self._config.r2_bucket)

            logger.info(
                "backup_uploaded",
                backup_id=backup_id,
                r2_key=r2_key,
                size_bytes=encrypted_size,
                checksum=checksum[:16],
            )

            return BackupMetadata(
                backup_id=backup_id,
                created_at=now,
                size_bytes=encrypted_size,
                compressed_size_bytes=compressed_size,
                original_size_bytes=original_size,
                brain_version=_get_brain_version(self._db_path),
                sovyx_version=_get_sovyx_version(),
                checksum=checksum,
                r2_key=r2_key,
            )
        finally:
            # Clean up temporary files
            for f in work_dir.iterdir():
                f.unlink(missing_ok=True)
            if tmp_dir is None:
                work_dir.rmdir()

    def restore_backup(
        self,
        backup_id: str,
        restore_dir: Path,
    ) -> RestoreResult:
        """Download, decrypt, decompress, and verify a backup.

        Steps:
            1. Find the backup by ID in R2 object listing.
            2. Download the encrypted blob.
            3. Decrypt with BackupCrypto.
            4. gzip decompress.
            5. Write to ``restore_dir/brain.db``.
            6. Run ``PRAGMA integrity_check`` on the restored DB.

        Args:
            backup_id: The backup identifier (hex UUID).
            restore_dir: Directory to write the restored ``brain.db`` into.

        Returns:
            RestoreResult with integrity check status.

        Raises:
            FileNotFoundError: If the backup_id is not found in R2.
            ValueError: If the encrypted data cannot be decrypted.
        """
        # Find the matching R2 key
        r2_key = self._find_backup_key(backup_id)
        if r2_key is None:
            msg = f"Backup not found: {backup_id}"
            raise FileNotFoundError(msg)

        logger.info("backup_download_started", backup_id=backup_id, r2_key=r2_key)

        # Download
        encrypted = self._r2.download_bytes(r2_key, self._config.r2_bucket)

        # Decrypt
        compressed = BackupCrypto.decrypt(encrypted, self._password)

        # Decompress
        raw_data = gzip.decompress(compressed)

        # Write restored DB
        restore_dir.mkdir(parents=True, exist_ok=True)
        restored_path = restore_dir / "brain.db"
        restored_path.write_bytes(raw_data)

        # Integrity check
        integrity_ok = _check_integrity(restored_path)

        logger.info(
            "backup_restored",
            backup_id=backup_id,
            size_bytes=len(raw_data),
            integrity_ok=integrity_ok,
        )

        return RestoreResult(
            backup_id=backup_id,
            restored_at=datetime.now(tz=UTC),
            original_size_bytes=len(raw_data),
            integrity_ok=integrity_ok,
        )

    def list_backups(self) -> list[BackupInfo]:
        """List available backups from R2 storage.

        Returns:
            List of BackupInfo, sorted by creation time (newest first).
        """
        objects = self._r2.list_objects(self._prefix + "/", self._config.r2_bucket)
        backups: list[BackupInfo] = []

        for obj in objects:
            key: str = obj["Key"]
            # Parse backup_id from key: {prefix}/{timestamp}_{backup_id}.enc.gz
            filename = key.rsplit("/", maxsplit=1)[-1]
            parts = filename.replace(".enc.gz", "").split("_", maxsplit=1)
            if len(parts) != 2:  # noqa: PLR2004
                continue

            timestamp_str, bid = parts
            try:
                created = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=UTC,
                )
            except ValueError:
                continue

            backups.append(
                BackupInfo(
                    backup_id=bid,
                    created_at=created,
                    size_bytes=obj.get("Size", 0),
                    r2_key=key,
                ),
            )

        # Sort newest first
        backups.sort(key=lambda b: b.created_at, reverse=True)
        return backups

    def _find_backup_key(self, backup_id: str) -> str | None:
        """Find R2 key for a backup ID."""
        objects = self._r2.list_objects(self._prefix + "/", self._config.r2_bucket)
        for obj in objects:
            key: str = obj["Key"]
            if backup_id in key:
                return key
        return None


def _check_integrity(db_path: Path) -> bool:
    """Run PRAGMA integrity_check on a SQLite database.

    Args:
        db_path: Path to the database file.

    Returns:
        ``True`` if the integrity check passes, ``False`` otherwise.
    """
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()
            return result is not None and result[0] == "ok"
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False
