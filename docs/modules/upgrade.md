# Module: upgrade

## What it does

`sovyx.upgrade` manages everything that happens outside the normal runtime loop: diagnostic health checks (`sovyx doctor`), schema migrations, data import/export (GDPR Art. 20), conversation importers (ChatGPT, Claude, Gemini, Grok), Obsidian vault import, and blue-green zero-downtime upgrades.

## Key classes

| Name | Responsibility |
|---|---|
| `Doctor` | 10+ diagnostic checks (DB, schema, disk, RAM, ports, Python, deps). |
| `MindExporter` | Export to SMF directory or `.sovyx-mind` ZIP archive. |
| `MindImporter` | Import from SMF or archive with re-scoring. |
| `BackupManager` | Pre-upgrade snapshot + rollback. |
| `BlueGreenUpgrader` | Zero-downtime schema upgrades. |
| `ChatGPTImporter` | Parse ChatGPT JSON export → Episodes + Concepts. |
| `ClaudeImporter` | Parse Claude export → Episodes + Concepts. |
| `GeminiImporter` | Parse Gemini export → Episodes + Concepts. |
| `GrokImporter` | Parse Grok export → Episodes + Concepts. |
| `ObsidianImporter` | Parse Obsidian vault (frontmatter, wiki links, nested tags). |
| `ImportProgressTracker` | Async background job tracking with polling endpoint. |

## Conversation importers

Five platforms supported. Each importer reads a platform-specific export file, runs a summary-first LLM encoder (one fast-model call per conversation), and writes Episodes + Concepts to the brain. Re-importing the same archive is deduplicated via SHA-256 on the `conversation_imports` table.

```bash
curl -X POST -H "Authorization: Bearer $(sovyx token)" \
     -F platform=chatgpt -F file=@conversations.json \
     http://localhost:7777/api/import/conversations
# 202 Accepted — poll /api/import/{job_id}/progress
```

| Platform | Format | Since |
|---|---|---|
| ChatGPT | JSON (`conversations.json`) | v0.11.3 |
| Claude | JSON | v0.11.4 |
| Gemini | JSON | v0.11.5 |
| Grok | JSON | v0.12.0 |
| Obsidian | Markdown vault (ZIP) | v0.12.0 |

The Obsidian importer reads YAML frontmatter, resolves `[[wiki links]]` via two-pass name resolution, and expands nested tags (`#project/alpha/beta`) into a PART_OF chain.

## Doctor

`sovyx doctor` runs 10+ diagnostic checks and reports PASS / WARN / FAIL for each:

- Database integrity (PRAGMA integrity_check)
- Schema version (migrations up to date)
- Disk space (minimum free threshold)
- Memory usage (RSS vs available)
- LLM provider reachability
- Port availability (dashboard :7777)
- Python version (3.11+)
- Dependency versions
- Model files (ONNX embeddings present)
- Configuration validity

## Export / import

The Sovyx Mind Format (SMF) is a directory of Markdown concept files with YAML frontmatter, JSON relation metadata, and conversation episode files. The `.sovyx-mind` archive is a ZIP containing `brain.db` + `mind.yaml` + `manifest.json`.

```bash
sovyx mind export my-mind --format smf --output ./export/
sovyx mind import ./export/smf/
```

## Configuration

No dedicated config — operations are driven by CLI flags and API parameters.

## Roadmap

- InterMindBridge for multi-instance sync.
- Cursor pagination for large export datasets.

## See also

- Source: `src/sovyx/upgrade/`, `src/sovyx/upgrade/conv_import/`, `src/sovyx/upgrade/vault_import/`.
- Tests: `tests/unit/upgrade/`.
- Related: [`persistence`](./persistence.md) (migrations), [`brain`](./brain.md) (import target), [`cli`](./cli.md) (`sovyx doctor` command).
