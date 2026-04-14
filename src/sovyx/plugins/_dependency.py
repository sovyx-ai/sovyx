"""Plugin dependency resolution — topological sort.

Extracted from manager.py. Used by ``PluginManager.load_all`` to load
plugins in dependency order (dependencies first).
"""

from __future__ import annotations

from sovyx.plugins._manager_types import PluginError


def _topological_sort(plugins: dict[str, list[str]]) -> list[str]:
    """Topological sort of plugins by dependencies (Kahn's algorithm).

    Args:
        plugins: Mapping of ``plugin_name -> list[dependency_name]``.

    Returns:
        Ordered list of plugin names with dependencies before dependents.

    Raises:
        PluginError: Circular dependency detected.
    """
    in_degree: dict[str, int] = {name: 0 for name in plugins}
    graph: dict[str, list[str]] = {name: [] for name in plugins}

    for name, deps in plugins.items():
        for dep in deps:
            if dep in plugins:
                graph[dep].append(name)
                in_degree[name] += 1

    queue = [n for n in plugins if in_degree[n] == 0]
    result: list[str] = []

    while queue:
        queue.sort()  # deterministic order
        node = queue.pop(0)
        result.append(node)
        for dependent in graph[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(result) != len(plugins):
        missing = set(plugins) - set(result)
        msg = f"Circular dependency detected among: {sorted(missing)}"
        raise PluginError(msg)

    return result
