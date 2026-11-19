# FreeQuantDB

- Public distribution starts with already standardized Parquet. Keep actual data origins, access details, input examples, mapping, cleaning, and generation procedures private. This applies to code, documentation, catalog metadata, tests, logs, commit messages, branches, and release artifacts.
- Keep the root `tools/` ignored and out of all public Git history, builds, archives, and distribution packages. Private preparation may output standardized Parquet. File publication and network distribution are separate explicit operations.
- Public catalog objects contain only the information needed to locate, display, download, or verify the packages. Use synthetic fixtures in the public repository and development tests.
- The Python package, import name, and command are `fqdb`. Supported Python starts at CPython 3.11. Use the dedicated `.venvs/FQDB` environment for local development.
- Keep `catalog/` and `data/` paths mirrored. Store native BT metadata and historical identities under private runtime state. Preserve the original info-hash and contents when a catalog moves.
- A normal `import fqdb` and normal CLI work activate the same background for the selected data root. The console launcher must parse control and help commands before any activation. Worker imports must remain passive. Use `FQDB_AUTOSTART=0` for test setup and explicitly offline scripts. Do not install startup services.
- A saved data root includes its storage identity. Stop on a missing or replaced root before creating runtime files. Never redirect a saved root to a different disk implicitly.
- Validate and stage only task-owned changes. Commit, pull with `--ff-only`, and push authorized changes. Keep runtime data and confidential files outside the public repository.
