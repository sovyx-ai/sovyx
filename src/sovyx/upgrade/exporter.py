"""MindExporter — export brain data to SMF and .sovyx-mind formats.

Provides :class:`MindExporter` for exporting a Mind's brain data
(concepts, episodes, relations) to the Sovyx Memory Format (SMF)
directory structure or to a ``.sovyx-mind`` ZIP archive.

Ref: SPE-028 §5–5B — Mind export, SMF format, GDPR Art. 20.
"""

from __future__ import annotations

import datetime
import json
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.persistence.pool import DatabasePool

logger = get_logger(__name__)

# ── Data classes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExportManifest:
    """Manifest describing an export.

    Attributes:
        format_version: SMF format version.
        sovyx_version: Sovyx application version that created the export.
        mind_id: Identifier of the exported Mind.
        mind_name: Human-readable name of the Mind.
        exported_at: ISO-8601 timestamp of the export.
        statistics: Summary counts (concepts, episodes, relations).
    """

    format_version: int
    sovyx_version: str
    mind_id: str
    mind_name: str
    exported_at: str
    statistics: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize manifest to a plain dict."""
        return {
            "format_version": self.format_version,
            "sovyx_version": self.sovyx_version,
            "mind_id": self.mind_id,
            "mind_name": self.mind_name,
            "exported_at": self.exported_at,
            "statistics": dict(self.statistics),
            "gdpr": {
                "format": "structured, commonly used, machine-readable",
                "standard": "GDPR Art. 20 compliant",
                "license": "User owns all data. No restrictions on portability.",
            },
        }


@dataclass
class ExportInfo:
    """Result of an export operation.

    Attributes:
        path: Location of the exported data (file or directory).
        manifest: The export manifest.
        format: ``"smf"`` or ``"sovyx-mind"``.
        size_bytes: Approximate size in bytes (for archives).
    """

    path: Path
    manifest: ExportManifest
    format: str
    size_bytes: int = 0


# ── MindExporter ────────────────────────────────────────────────────

# Current SMF format version
_SMF_VERSION = 1

# Maximum number of rows fetched in a single query batch
_BATCH_SIZE = 500


class MindExporter:
    """Export a Mind's brain data to portable formats.

    Supports two output formats:

    * **SMF** (Sovyx Memory Format) — human-readable directory of
      Markdown + YAML files. GDPR Art. 20 compliant.
    * **.sovyx-mind** — ZIP archive containing ``manifest.json``,
      ``mind.yaml`` (if provided), and a VACUUM copy of brain.db.

    Args:
        pool: Database pool for reading brain data.
        sovyx_version: Current application version string.
    """

    def __init__(
        self,
        pool: DatabasePool,
        sovyx_version: str = "0.5.0",
    ) -> None:
        self._pool = pool
        self._sovyx_version = sovyx_version

    # ── Public API ──────────────────────────────────────────────

    async def export_smf(
        self,
        mind_id: str,
        output_dir: Path,
        *,
        mind_name: str = "",
        include_conversations: bool = True,
    ) -> ExportInfo:
        """Export a Mind to an SMF directory.

        Creates the directory structure described in SPE-028 §5B.2
        with one Markdown file per concept and conversation.

        Args:
            mind_id: Identifier of the Mind to export.
            output_dir: Target directory (will be created).
            mind_name: Human-readable name (defaults to *mind_id*).
            include_conversations: Whether to export episodes.

        Returns:
            :class:`ExportInfo` with export details.
        """
        mind_name = mind_name or mind_id
        output_dir.mkdir(parents=True, exist_ok=True)

        concepts = await self._fetch_concepts(mind_id)
        relations = await self._fetch_relations(mind_id)
        episodes = await self._fetch_episodes(mind_id) if include_conversations else []

        # Build manifest
        manifest = ExportManifest(
            format_version=_SMF_VERSION,
            sovyx_version=self._sovyx_version,
            mind_id=mind_id,
            mind_name=mind_name,
            exported_at=datetime.datetime.now(tz=datetime.UTC).isoformat(),
            statistics={
                "total_concepts": len(concepts),
                "total_episodes": len(episodes),
                "total_relations": len(relations),
            },
        )

        # Write manifest
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Write concepts
        concepts_dir = output_dir / "concepts"
        concepts_dir.mkdir(exist_ok=True)
        for concept in concepts:
            self._write_concept_file(concepts_dir, concept)

        # Write relations (synapses)
        metadata_dir = output_dir / "metadata"
        metadata_dir.mkdir(exist_ok=True)
        self._write_relations_file(metadata_dir, relations)

        # Write episodes (conversations)
        if episodes:
            convos_dir = output_dir / "conversations"
            convos_dir.mkdir(exist_ok=True)
            for episode in episodes:
                self._write_episode_file(convos_dir, episode)

        logger.info(
            "smf_export_complete",
            mind_id=mind_id,
            concepts=len(concepts),
            episodes=len(episodes),
            relations=len(relations),
        )

        return ExportInfo(
            path=output_dir,
            manifest=manifest,
            format="smf",
        )

    async def export_archive(
        self,
        mind_id: str,
        output_path: Path,
        *,
        mind_name: str = "",
        mind_config: dict[str, Any] | None = None,
    ) -> ExportInfo:
        """Export a Mind to a ``.sovyx-mind`` ZIP archive.

        The archive contains:

        * ``manifest.json`` — export metadata.
        * ``mind.yaml`` — Mind configuration (if provided).
        * ``brain.db`` — Full database snapshot via ``VACUUM INTO``.

        Args:
            mind_id: Identifier of the Mind to export.
            output_path: Target ``.sovyx-mind`` file path.
            mind_name: Human-readable name (defaults to *mind_id*).
            mind_config: Optional Mind configuration dict to include.

        Returns:
            :class:`ExportInfo` with export details.
        """
        mind_name = mind_name or mind_id
        output_path.parent.mkdir(parents=True, exist_ok=True)

        manifest = ExportManifest(
            format_version=_SMF_VERSION,
            sovyx_version=self._sovyx_version,
            mind_id=mind_id,
            mind_name=mind_name,
            exported_at=datetime.datetime.now(tz=datetime.UTC).isoformat(),
        )

        # Create VACUUM copy for the archive
        db_copy = output_path.parent / f"_export_{mind_id}.db"
        try:
            async with self._pool.write() as conn:
                await conn.execute(f"VACUUM INTO '{db_copy}'")

            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
                )

                if mind_config is not None:
                    zf.writestr(
                        "mind.yaml",
                        json.dumps(mind_config, indent=2, ensure_ascii=False),
                    )

                zf.write(db_copy, "brain.db")

            size_bytes = output_path.stat().st_size
        finally:
            if db_copy.exists():
                db_copy.unlink()

        logger.info(
            "archive_export_complete",
            mind_id=mind_id,
            size_bytes=size_bytes,
        )

        return ExportInfo(
            path=output_path,
            manifest=manifest,
            format="sovyx-mind",
            size_bytes=size_bytes,
        )

    # ── Database queries ────────────────────────────────────────

    async def _fetch_concepts(self, mind_id: str) -> list[dict[str, Any]]:
        """Fetch all concepts for a Mind."""
        rows: list[dict[str, Any]] = []
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT id, mind_id, name, content, category, importance, "
                "confidence, access_count, last_accessed, emotional_valence, "
                "source, metadata, created_at, updated_at, "
                "emotional_arousal, emotional_dominance "
                "FROM concepts WHERE mind_id = ? ORDER BY created_at",
                (mind_id,),
            )
            for row in await cursor.fetchall():
                rows.append(
                    {
                        "id": row[0],
                        "mind_id": row[1],
                        "name": row[2],
                        "content": row[3],
                        "category": row[4],
                        "importance": row[5],
                        "confidence": row[6],
                        "access_count": row[7],
                        "last_accessed": row[8],
                        "emotional_valence": row[9],
                        "source": row[10],
                        "metadata": row[11],
                        "created_at": row[12],
                        "updated_at": row[13],
                        "emotional_arousal": row[14],
                        "emotional_dominance": row[15],
                    }
                )
        return rows

    async def _fetch_episodes(self, mind_id: str) -> list[dict[str, Any]]:
        """Fetch all episodes for a Mind."""
        rows: list[dict[str, Any]] = []
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT id, mind_id, conversation_id, user_input, "
                "assistant_response, summary, importance, "
                "emotional_valence, emotional_arousal, "
                "concepts_mentioned, metadata, created_at, "
                "emotional_dominance "
                "FROM episodes WHERE mind_id = ? ORDER BY created_at",
                (mind_id,),
            )
            for row in await cursor.fetchall():
                rows.append(
                    {
                        "id": row[0],
                        "mind_id": row[1],
                        "conversation_id": row[2],
                        "user_input": row[3],
                        "assistant_response": row[4],
                        "summary": row[5],
                        "importance": row[6],
                        "emotional_valence": row[7],
                        "emotional_arousal": row[8],
                        "concepts_mentioned": row[9],
                        "metadata": row[10],
                        "created_at": row[11],
                        "emotional_dominance": row[12],
                    }
                )
        return rows

    async def _fetch_relations(self, mind_id: str) -> list[dict[str, Any]]:
        """Fetch all relations for concepts belonging to a Mind."""
        rows: list[dict[str, Any]] = []
        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT r.id, r.source_id, r.target_id, r.relation_type, "
                "r.weight, r.co_occurrence_count, r.last_activated, r.created_at "
                "FROM relations r "
                "INNER JOIN concepts c ON r.source_id = c.id "
                "WHERE c.mind_id = ? ORDER BY r.created_at",
                (mind_id,),
            )
            for row in await cursor.fetchall():
                rows.append(
                    {
                        "id": row[0],
                        "source_id": row[1],
                        "target_id": row[2],
                        "relation_type": row[3],
                        "weight": row[4],
                        "co_occurrence_count": row[5],
                        "last_activated": row[6],
                        "created_at": row[7],
                    }
                )
        return rows

    # ── File writers ────────────────────────────────────────────

    @staticmethod
    def _write_concept_file(
        parent_dir: Path,
        concept: dict[str, Any],
    ) -> None:
        """Write a single concept as a Markdown file with YAML frontmatter."""
        category = concept.get("category", "fact")
        cat_dir = parent_dir / category
        cat_dir.mkdir(exist_ok=True)

        # Sanitize name for filename
        safe_name = _sanitize_filename(concept.get("name", concept["id"]))
        filepath = cat_dir / f"{safe_name}.md"

        # Build YAML frontmatter
        fm_lines = [
            "---",
            f'id: "{concept["id"]}"',
            f"type: {category}",
            f"category: {category}",
            f'created: "{concept.get("created_at", "")}"',
            f'updated: "{concept.get("updated_at", "")}"',
            f'source: "{concept.get("source", "conversation")}"',
            f"confidence: {concept.get('confidence', 0.5)}",
            f"importance: {concept.get('importance', 0.5)}",
            f"emotional_valence: {concept.get('emotional_valence', 0.0)}",
            f"access_count: {concept.get('access_count', 0)}",
            "---",
            "",
            f"# {concept.get('name', '')}",
            "",
            concept.get("content", ""),
        ]

        filepath.write_text("\n".join(fm_lines), encoding="utf-8")

    @staticmethod
    def _write_relations_file(
        metadata_dir: Path,
        relations: list[dict[str, Any]],
    ) -> None:
        """Write relations (synapses) as a JSON file."""
        filepath = metadata_dir / "synapses.json"
        serializable = []
        for rel in relations:
            serializable.append(
                {
                    "id": rel["id"],
                    "source_id": rel["source_id"],
                    "target_id": rel["target_id"],
                    "relation_type": rel["relation_type"],
                    "weight": rel["weight"],
                    "co_occurrence_count": rel["co_occurrence_count"],
                    "last_activated": str(rel.get("last_activated", "")),
                    "created_at": str(rel.get("created_at", "")),
                }
            )
        filepath.write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _write_episode_file(
        convos_dir: Path,
        episode: dict[str, Any],
    ) -> None:
        """Write a single episode as a Markdown file."""
        safe_id = _sanitize_filename(episode.get("id", "unknown"))
        created = episode.get("created_at", "")
        filepath = convos_dir / f"{safe_id}.md"

        fm_lines = [
            "---",
            f'id: "{episode["id"]}"',
            f'conversation_id: "{episode.get("conversation_id", "")}"',
            f'created: "{created}"',
            f"importance: {episode.get('importance', 0.5)}",
            f"emotional_valence: {episode.get('emotional_valence', 0.0)}",
            f"emotional_arousal: {episode.get('emotional_arousal', 0.0)}",
            "---",
            "",
            f"# Episode — {created}",
            "",
            f"**User:** {episode.get('user_input', '')}",
            "",
            f"**Assistant:** {episode.get('assistant_response', '')}",
        ]

        summary = episode.get("summary")
        if summary:
            fm_lines.extend(["", f"**Summary:** {summary}"])

        filepath.write_text("\n".join(fm_lines), encoding="utf-8")


# ── Utilities ───────────────────────────────────────────────────────


def _sanitize_filename(name: str) -> str:
    """Create a safe filename from an arbitrary string.

    Replaces unsafe characters with hyphens and truncates to 100 chars.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)
    # Collapse multiple hyphens
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:100] or "unnamed"
