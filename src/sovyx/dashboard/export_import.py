"""Export/Import API helpers for the dashboard.

Wraps :class:`MindExporter`/:class:`MindImporter` for use by
dashboard REST endpoints.

Ref: SPE-028 §5–5B — Mind export/import, GDPR Art. 20.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


async def export_mind(registry: ServiceRegistry, mind_id: str) -> Path:
    """Export a mind as a ``.sovyx-mind`` ZIP archive.

    Args:
        registry: Service registry for resolving database pool.
        mind_id: Identifier of the mind to export.

    Returns:
        Path to the temporary archive file.  Caller must clean up.

    Raises:
        RuntimeError: If database is unavailable or export fails.
    """
    from sovyx.engine.types import MindId
    from sovyx.persistence.manager import DatabaseManager

    if not registry.is_registered(DatabaseManager):
        msg = "Database manager not available"
        raise RuntimeError(msg)

    db = await registry.resolve(DatabaseManager)
    pool = db.get_brain_pool(MindId(mind_id))

    from sovyx import __version__
    from sovyx.upgrade.exporter import MindExporter

    exporter = MindExporter(pool=pool, sovyx_version=__version__)

    with tempfile.NamedTemporaryFile(
        suffix=".sovyx-mind",
        prefix=f"export-{mind_id}-",
        delete=False,
    ) as tmp:
        output_path = Path(tmp.name)

    # Attach mind config if available
    mind_config: dict[str, Any] | None = None
    try:
        from sovyx.mind.personality import PersonalityEngine

        if registry.is_registered(PersonalityEngine):
            engine = await registry.resolve(PersonalityEngine)
            if hasattr(engine.config, "to_dict"):
                mind_config = engine.config.to_dict()
    except Exception:  # noqa: BLE001
        pass

    info = await exporter.export_archive(
        mind_id=mind_id,
        output_path=output_path,
        mind_name=mind_id,
        mind_config=mind_config,
    )

    stats = info.manifest.statistics
    logger.info(
        "export_completed",
        mind_id=mind_id,
        path=str(output_path),
        concepts=stats.get("concepts", 0),
        episodes=stats.get("episodes", 0),
    )
    return output_path


async def import_mind(
    registry: ServiceRegistry,
    archive_path: Path,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Import a ``.sovyx-mind`` archive.

    Args:
        registry: Service registry for resolving database pool.
        archive_path: Path to the uploaded archive.
        overwrite: If True, replace existing data.

    Returns:
        Dict with import results (concepts_imported, episodes_imported, etc.)
    """
    from sovyx.persistence.manager import DatabaseManager
    from sovyx.upgrade.importer import MindImporter

    if not registry.is_registered(DatabaseManager):
        msg = "Database manager not available"
        raise RuntimeError(msg)

    db = await registry.resolve(DatabaseManager)
    # Importer uses its own pool internally; we pass the system pool
    importer = MindImporter(pool=db.get_system_pool())
    info = await importer.import_archive(
        archive_path=archive_path,
        overwrite=overwrite,
    )

    logger.info(
        "import_completed",
        mind_id=info.mind_id,
        concepts=info.concepts_imported,
        episodes=info.episodes_imported,
    )

    return {
        "mind_id": info.mind_id,
        "concepts_imported": info.concepts_imported,
        "episodes_imported": info.episodes_imported,
        "relations_imported": info.relations_imported,
        "warnings": info.warnings,
    }
