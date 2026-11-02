# Stage 1 service implementation

Spec: `docs/stage-1-plan.md`; execute in this checkout without Git or agents.
Python 3.13.14, runtime standard library only. Source/test/report writes stay in
the assigned paths; packaging additionally writes the explicitly requested
`build/service` and `dist/stage-1/service` trees.

- [x] Write and run storage tests: defaults, typed patch validation, atomic save
  failure, bounded retry, exact corrupt-byte backups, SQLite initialization and
  rollback. Implement `service/settings.py` and `service/database.py`.
- [x] Write and run actual pipe tests, then implement `service/protocol.py`,
  `service/runtime.py`, and `service/main.py`: incremental binary line framing,
  strict UTF-8/JSON/envelopes, request IDs, settings, unavailable features,
  cancellation, shutdown and EOF. Use synchronous processing as the sole writer.
- [x] Run live save-failure/recovery, corrupt-config, and duplicate-writer tests.
  Add a data-directory lifetime lock so a second service cannot race config saves.
- [x] Implement `tools/build_service.py` using PyInstaller onedir/console;
  build and run the same subprocess contract tests against a copy in a Chinese
  path with spaces, without a Python interpreter on PATH.
- [x] Record exact commands, results, integration contract and limitations in
  `docs/reports/stage-1/service.md`.

Settings JSON is authoritative; SQLite stores schema metadata and empty future
conversation/message/memory tables, not a second copy of mutable settings.
Atomic writes fsync a sibling temporary file before bounded os.replace retries;
in-memory settings advance only after replacement. Invalid files are copied to a
unique timestamp backup before defaults are written. Backup/startup failure is
an explicit protocol error and bounded nonzero exit. Oversize frames produce one
bounded error and exit; malformed bounded frames resynchronize at the next LF.
