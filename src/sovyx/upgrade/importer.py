"""MindImporter — import brain data from SMF and .sovyx-mind formats.

Provides :class:`MindImporter` for importing a Mind's brain data from
the Sovyx Memory Format (SMF) directory or from a ``.sovyx-mind`` ZIP
archive back into a Sovyx database.

Ref: SPE-028 §5–5B — Mind import, SMF format, GDPR Art. 20.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from sovyx.engine.errors import MigrationError
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sovyx.persistence.pool import DatabasePool
    from sovyx.upgrade.schema import MigrationRunner, UpgradeMigration

logger = get_logger(__name__)

# ── Errors ──────────────────────────────────────────────────────────


class ImportValidationError(MigrationError):
    """Raised when import data fails validation."""


# ── Data classes ────────────────────────────────────────────────────


@dataclass
class ImportInfo:
    """Result of an import operation.

    Attributes:
        mind_id: Identifier of the imported Mind.
        source_format: ``"smf"`` or ``"sovyx-mind"``.
        concepts_imported: Number of concepts imported.
        episodes_imported: Number of episodes imported.
        relations_imported: Number of relations imported.
        migrations_applied: Descriptions of schema migrations run.
        warnings: Non-fatal issues encountered.
    """

    mind_id: str
    source_format: str
    concepts_imported: int = 0
    episodes_imported: int = 0
    relations_imported: int = 0
    migrations_applied: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── MindImporter ────────────────────────────────────────────────────

# Supported format versions
_MAX_FORMAT_VERSION = 1


class MindImporter:
    """Import a Mind's brain data from portable formats.

    Supports two input formats:

    * **SMF** (Sovyx Memory Format) — directory of Markdown + JSON
      files produced by :class:`MindExporter.export_smf`.
    * **.sovyx-mind** — ZIP archive produced by
      :class:`MindExporter.export_archive`.

    Args:
        pool: Database pool for writing imported data.
        migration_runner: Optional runner for schema migrations after
            archive import.
        migrations: Known migrations to apply (used with archive import).
    """

    def __init__(
        self,
        pool: DatabasePool,
        migration_runner: MigrationRunner | None = None,
        migrations: Sequence[UpgradeMigration] | None = None,
    ) -> None:
        self._pool = pool
        self._migration_runner = migration_runner
        self._migrations = migrations or []

    # ── Public API ──────────────────────────────────────────────

    async def import_smf(
        self,
        smf_dir: Path,
        *,
        overwrite: bool = False,
    ) -> ImportInfo:
        """Import a Mind from an SMF directory.

        Reads ``manifest.json``, concepts, relations, and conversations
        from the SMF directory structure and inserts them into the
        database.

        Args:
            smf_dir: Path to the SMF directory.
            overwrite: If ``True``, overwrite existing concepts with the
                same ID. Otherwise skip duplicates.

        Returns:
            :class:`ImportInfo` with import statistics.

        Raises:
            ImportValidationError: If the manifest is missing or invalid.
            FileNotFoundError: If *smf_dir* does not exist.
        """
        if not smf_dir.is_dir():
            msg = f"SMF directory not found: {smf_dir}"
            raise FileNotFoundError(msg)

        manifest = self._load_manifest(smf_dir / "manifest.json")
        mind_id = manifest.get("mind_id", "")
        if not mind_id:
            msg = "Manifest missing 'mind_id'"
            raise ImportValidationError(msg)

        info = ImportInfo(mind_id=mind_id, source_format="smf")

        # Check for existing data
        if not overwrite:
            existing = await self._count_concepts(mind_id)
            if existing > 0:
                msg = (
                    f"Mind '{mind_id}' already has {existing} concepts. "
                    "Use overwrite=True to replace."
                )
                raise ImportValidationError(msg)

        # Import concepts
        concepts_dir = smf_dir / "concepts"
        if concepts_dir.is_dir():
            info.concepts_imported = await self._import_concepts_from_dir(
                concepts_dir, mind_id
            )

        # Import relations
        synapses_file = smf_dir / "metadata" / "synapses.json"
        if synapses_file.is_file():
            info.relations_imported = await self._import_relations_from_file(
                synapses_file
            )

        # Import conversations
        convos_dir = smf_dir / "conversations"
        if convos_dir.is_dir():
            info.episodes_imported = await self._import_episodes_from_dir(
                convos_dir, mind_id
            )

        logger.info(
            "smf_import_complete",
            mind_id=mind_id,
            concepts=info.concepts_imported,
            episodes=info.episodes_imported,
            relations=info.relations_imported,
        )

        return info

    async def import_archive(
        self,
        archive_path: Path,
        *,
        overwrite: bool = False,
        db_restore_dir: Path | None = None,
    ) -> ImportInfo:
        """Import a Mind from a ``.sovyx-mind`` ZIP archive.

        Extracts the archive, validates the manifest, and restores
        ``brain.db`` to *db_restore_dir* (defaulting to
        ``~/.sovyx/minds/<mind_id>/``). If a migration runner is
        configured, pending schema migrations are applied to the
        restored database.

        Args:
            archive_path: Path to the ``.sovyx-mind`` file.
            overwrite: If ``True``, overwrite an existing database.
            db_restore_dir: Target directory for the restored DB.

        Returns:
            :class:`ImportInfo` with import details.

        Raises:
            ImportValidationError: If the archive or manifest is invalid.
            FileNotFoundError: If *archive_path* does not exist.
        """
        if not archive_path.is_file():
            msg = f"Archive not found: {archive_path}"
            raise FileNotFoundError(msg)

        with zipfile.ZipFile(archive_path, "r") as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                msg = "Archive missing manifest.json"
                raise ImportValidationError(msg)

            manifest = json.loads(zf.read("manifest.json"))
            self._validate_manifest(manifest)

            mind_id = manifest["mind_id"]

            # Determine target directory
            target_dir = db_restore_dir or (
                Path.home() / ".sovyx" / "minds" / mind_id
            )
            target_db = target_dir / "brain.db"

            if target_db.exists() and not overwrite:
                msg = (
                    f"Mind '{mind_id}' database already exists at "
                    f"{target_db}. Use overwrite=True to replace."
                )
                raise ImportValidationError(msg)

            target_dir.mkdir(parents=True, exist_ok=True)

            # Extract brain.db
            if "brain.db" in names:
                db_data = zf.read("brain.db")
                target_db.write_bytes(db_data)
            else:
                msg = "Archive missing brain.db"
                raise ImportValidationError(msg)

            # Extract mind.yaml if present
            if "mind.yaml" in names:
                config_data = zf.read("mind.yaml")
                (target_dir / "mind.yaml").write_bytes(config_data)

        info = ImportInfo(mind_id=mind_id, source_format="sovyx-mind")

        # Run pending schema migrations if runner is available
        if self._migration_runner is not None and self._migrations:
            report = await self._migration_runner.run(self._migrations)
            info.migrations_applied = report.applied

        logger.info(
            "archive_import_complete",
            mind_id=mind_id,
            db_path=str(target_db),
            migrations=len(info.migrations_applied),
        )

        return info

    # ── Manifest helpers ────────────────────────────────────────

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, Any]:
        """Load and validate an SMF manifest."""
        if not path.is_file():
            msg = f"Manifest not found: {path}"
            raise ImportValidationError(msg)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = f"Invalid manifest JSON: {exc}"
            raise ImportValidationError(msg) from exc

        if not isinstance(data, dict):
            msg = "Manifest must be a JSON object"
            raise ImportValidationError(msg)

        return data

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> None:
        """Validate a manifest dict for required fields and version."""
        version = manifest.get("format_version")
        if version is None:
            msg = "Manifest missing 'format_version'"
            raise ImportValidationError(msg)

        if not isinstance(version, int) or version > _MAX_FORMAT_VERSION:
            msg = (
                f"Unsupported format version: {version}. "
                f"Max supported: {_MAX_FORMAT_VERSION}"
            )
            raise ImportValidationError(msg)

        if not manifest.get("mind_id"):
            msg = "Manifest missing 'mind_id'"
            raise ImportValidationError(msg)

    # ── Database helpers ────────────────────────────────────────

    async def _count_concepts(self, mind_id: str) -> int:
        """Count existing concepts for a mind."""
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE mind_id = ?",
                (mind_id,),
            )
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def _import_concepts_from_dir(
        self,
        concepts_dir: Path,
        mind_id: str,
    ) -> int:
        """Import concept Markdown files from an SMF concepts directory."""
        count = 0
        for md_file in concepts_dir.rglob("*.md"):
            concept = self._parse_concept_file(md_file, mind_id)
            if concept is not None:
                await self._insert_concept(concept)
                count += 1
        return count

    async def _import_relations_from_file(
        self,
        synapses_file: Path,
    ) -> int:
        """Import relations from a synapses JSON file."""
        try:
            data = json.loads(synapses_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return 0

        if not isinstance(data, list):
            return 0

        count = 0
        for rel in data:
            if isinstance(rel, dict) and "source_id" in rel and "target_id" in rel:
                await self._insert_relation(rel)
                count += 1
        return count

    async def _import_episodes_from_dir(
        self,
        convos_dir: Path,
        mind_id: str,
    ) -> int:
        """Import episode Markdown files from an SMF conversations directory."""
        count = 0
        for md_file in convos_dir.glob("*.md"):
            episode = self._parse_episode_file(md_file, mind_id)
            if episode is not None:
                await self._insert_episode(episode)
                count += 1
        return count

    async def _insert_concept(self, concept: dict[str, Any]) -> None:
        """Insert a single concept into the database."""
        metadata = concept.get("metadata", "")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)

        async with self._pool.write() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO concepts "
                "(id, mind_id, name, content, category, importance, "
                "confidence, access_count, emotional_valence, source, "
                "metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    concept["id"],
                    concept["mind_id"],
                    concept["name"],
                    concept.get("content", ""),
                    concept.get("category", "fact"),
                    concept.get("importance", 0.5),
                    concept.get("confidence", 0.5),
                    concept.get("access_count", 0),
                    concept.get("emotional_valence", 0.0),
                    concept.get("source", "import"),
                    metadata,
                    concept.get("created_at", ""),
                    concept.get("updated_at", ""),
                ),
            )
            await conn.commit()

    async def _insert_relation(self, rel: dict[str, Any]) -> None:
        """Insert a single relation into the database."""
        async with self._pool.write() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO relations "
                "(id, source_id, target_id, relation_type, weight, "
                "co_occurrence_count, last_activated, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rel.get("id", ""),
                    rel["source_id"],
                    rel["target_id"],
                    rel.get("relation_type", "related_to"),
                    rel.get("weight", 0.5),
                    rel.get("co_occurrence_count", 1),
                    rel.get("last_activated", ""),
                    rel.get("created_at", ""),
                ),
            )
            await conn.commit()

    async def _insert_episode(self, episode: dict[str, Any]) -> None:
        """Insert a single episode into the database."""
        concepts_mentioned = episode.get("concepts_mentioned", [])
        if isinstance(concepts_mentioned, list):
            concepts_mentioned = json.dumps(concepts_mentioned)

        metadata = episode.get("metadata", "")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)

        async with self._pool.write() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO episodes "
                "(id, mind_id, conversation_id, user_input, "
                "assistant_response, summary, importance, "
                "emotional_valence, emotional_arousal, "
                "concepts_mentioned, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    episode["id"],
                    episode["mind_id"],
                    episode.get("conversation_id", ""),
                    episode.get("user_input", ""),
                    episode.get("assistant_response", ""),
                    episode.get("summary"),
                    episode.get("importance", 0.5),
                    episode.get("emotional_valence", 0.0),
                    episode.get("emotional_arousal", 0.0),
                    concepts_mentioned,
                    metadata,
                    episode.get("created_at", ""),
                ),
            )
            await conn.commit()

    # ── File parsers ────────────────────────────────────────────

    @staticmethod
    def _parse_concept_file(
        filepath: Path,
        mind_id: str,
    ) -> dict[str, Any] | None:
        """Parse a concept Markdown file with YAML frontmatter.

        Returns ``None`` if parsing fails.
        """
        try:
            text = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        frontmatter, body = _split_frontmatter(text)
        if frontmatter is None:
            return None

        try:
            meta = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            return None

        if not isinstance(meta, dict):
            return None

        concept_id = meta.get("id", filepath.stem)
        name = body.split("\n")[0].lstrip("# ").strip() if body else filepath.stem

        return {
            "id": concept_id,
            "mind_id": mind_id,
            "name": name,
            "content": _extract_body_content(body),
            "category": meta.get("category", meta.get("type", "fact")),
            "importance": float(meta.get("importance", 0.5)),
            "confidence": float(meta.get("confidence", 0.5)),
            "access_count": int(meta.get("access_count", 0)),
            "emotional_valence": float(meta.get("emotional_valence", 0.0)),
            "source": meta.get("source", "import"),
            "created_at": str(meta.get("created", "")),
            "updated_at": str(meta.get("updated", "")),
        }

    @staticmethod
    def _parse_episode_file(
        filepath: Path,
        mind_id: str,
    ) -> dict[str, Any] | None:
        """Parse an episode Markdown file with YAML frontmatter.

        Returns ``None`` if parsing fails.
        """
        try:
            text = filepath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        frontmatter, body = _split_frontmatter(text)
        if frontmatter is None:
            return None

        try:
            meta = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            return None

        if not isinstance(meta, dict):
            return None

        # Extract user/assistant from body
        user_input = ""
        assistant_response = ""
        summary = None

        for line in body.split("\n"):
            if line.startswith("**User:**"):
                user_input = line[len("**User:**"):].strip()
            elif line.startswith("**Assistant:**"):
                assistant_response = line[len("**Assistant:**"):].strip()
            elif line.startswith("**Summary:**"):
                summary = line[len("**Summary:**"):].strip()

        return {
            "id": meta.get("id", filepath.stem),
            "mind_id": mind_id,
            "conversation_id": meta.get("conversation_id", ""),
            "user_input": user_input,
            "assistant_response": assistant_response,
            "summary": summary,
            "importance": float(meta.get("importance", 0.5)),
            "emotional_valence": float(meta.get("emotional_valence", 0.0)),
            "emotional_arousal": float(meta.get("emotional_arousal", 0.0)),
            "created_at": str(meta.get("created", "")),
        }


# ── Utilities ───────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split YAML frontmatter from Markdown body.

    Returns (frontmatter, body) where frontmatter is ``None``
    if no valid ``---`` delimiters are found.
    """
    text = text.strip()
    if not text.startswith("---"):
        return None, text

    # Find the closing ---
    end_idx = text.find("---", 3)
    if end_idx == -1:
        return None, text

    frontmatter = text[3:end_idx].strip()
    body = text[end_idx + 3:].strip()
    return frontmatter, body


def _extract_body_content(body: str) -> str:
    """Extract content from body, skipping the first heading line."""
    lines = body.split("\n")
    content_lines = []
    skip_first_heading = True
    for line in lines:
        if skip_first_heading and line.startswith("#"):
            skip_first_heading = False
            continue
        skip_first_heading = False
        content_lines.append(line)
    return "\n".join(content_lines).strip()
