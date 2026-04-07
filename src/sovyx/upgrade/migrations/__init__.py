"""Auto-discovered upgrade migrations.

Each migration file must be named with a numeric prefix (e.g.,
``001_initial.py``) and define a module-level ``MIGRATION`` attribute
of type :class:`~sovyx.upgrade.schema.UpgradeMigration`.

Discovery scans this package directory for files matching
``[0-9]*.py`` and imports them in sorted order.
"""

from __future__ import annotations
