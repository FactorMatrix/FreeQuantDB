# FreeQuantDB

- Public distribution starts with already standardized Parquet. Keep actual data origins, access details, input examples, mapping, cleaning, and generation procedures private. This applies to code, documentation, catalog metadata, tests, logs, commit messages, branches, and release artifacts.
- Keep the root `tools/` ignored and out of all public Git history, builds, archives, and distribution packages. Private preparation may output standardized Parquet. File publication and network distribution are separate explicit operations.
- Public catalog objects contain only the information needed to locate, display, download, or verify the packages. Use synthetic fixtures in the public repository and development tests.
- The Python package, import name, and command are `fqdb`. Supported Python starts at CPython 3.11. Use the dedicated `.venvs/FQDB` environment for local development.
- Keep `catalog/` and `data/` paths mirrored. Store native BT metadata and historical identities under private runtime state. Preserve the original info-hash and contents when a catalog moves.
- Help and offline queries are finite operations. Start a worker only for a user-activated gateway, active task, or explicitly enabled provider session. Do not install autostart services.
- Validate and stage only task-owned changes. Commit, pull with `--ff-only`, and push authorized changes. Keep runtime data and confidential files outside the public repository.
