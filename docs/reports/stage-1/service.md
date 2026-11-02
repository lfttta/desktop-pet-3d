# Stage 1 Python service

Implemented 2026-11-01 against `docs/stage-1-plan.md`, using Windows Python
3.13.14 (`D:\phthon lang\python.exe`), PyInstaller 6.21.0 and pytest 9.1.1.
No Git commands, commits, worktrees, agents or UI automation were used.

## Source and delivery

- `service/main.py`: CLI/stdio entry point; source launch is
  `& 'D:/phthon lang/python.exe' -u service/main.py --data-dir '<absolute-directory>'`.
- `service/protocol.py`, `jsonutil.py`, `runtime.py`: bounded binary UTF-8
  framing, strict decoding, synchronous dispatch and bounded stderr logging.
- `service/settings.py`: typed configuration, exact-byte recovery backups,
  atomic durable file replacement and last-successful in-memory state.
- `service/database.py`, `locking.py`: one SQLite connection and an OS-held
  data-directory lock; a second writer fails explicitly.
- `tools/build_service.py`: `--onedir --console --noupx` build. Intermediate
  files are under `build/service`; deliver the entire
  `dist/stage-1/service` directory, including `pet-service.exe` and `_internal`.
  App executables, native sources/binaries and other release directories are
  outside this script's output paths.
- Runtime source imports only the Python standard library. pytest and
  PyInstaller are development/build dependencies, not installed-user requirements.

## App integration contract

Launch `service/pet-service.exe` with Godot `OS.execute_with_pipe(..., false)`.
The executable intentionally uses the **console PE subsystem** to retain its
pipes; the parent hides the console. Read stdout as binary UTF-8 JSON lines and
stderr separately. The service uses `stdin.buffer` and `stdout.buffer.write`,
never Python's locale-default text encoding for protocol output.

Each request/reply has exactly `version`, `id`, `type`, `payload`. Version is
integer 1 (booleans/floats rejected), ID is a UTF-8 string, and payload is an
object. Requests do not require a prior hello. Replies are flushed immediately.
Valid envelopes retain their ID, including business errors and unsupported-version
errors; malformed JSON, invalid envelope field types/structure, and startup errors
use the empty string. Every reply payload is an object. No unsolicited ready event: send
`{"version":1,"id":"hello","type":"hello","payload":{"protocol_version":1}}`.

| Request | Response type | Payload |
| --- | --- | --- |
| `hello` | `ready` | `protocol_version:1`, `service_version:"0.1.0"`, `settings`, `warnings` (string array) |
| `settings.get` | `result` | `settings` |
| `settings.update` | `result` | Fully persisted `settings`; payload is the top-level patch itself |
| `chat.cancel`, `tts.cancel` | `cancelled` | `{}`; settings are unaffected |
| `chat.send`, `tts.synthesize`, `memory`, `memory.*` | `error` | `code:"not_available"`, `message` |
| `shutdown` | `result` | `{}`; exit 0 after reply; later queued commands are discarded |

Errors always have `payload:{"code":"...","message":"..."}`. Other codes:
`invalid_json`, `invalid_request`, `unsupported_version`, `invalid_settings`,
`save_failed`, `unknown_command`, `frame_too_large`, `incomplete_frame`,
`response_too_large`, `already_running`, `storage_unavailable`, `invalid_arguments`.

Bounded malformed lines return errors and resume at the next LF. The line limit
is 1,048,576 bytes excluding LF; CRLF is supported (CR counts toward the limit).
Oversize input emits one `frame_too_large` then exits 2 without waiting for LF.
EOF exits 0; an unterminated last command is discarded with `incomplete_frame`.
Duplicate JSON keys, NaN/Infinity/overflowing floats and nesting over 64 levels
are rejected. IDs are limited to 256 UTF-8 bytes and command names to 128 bytes
to keep error responses bounded. stderr is capped at 8 KiB for the process
lifetime, with short fixed diagnostics and no request bodies or credentials.

## Settings and storage

Default data path is `%LOCALAPPDATA%/Pet3D`; `--data-dir` overrides it. Files:
`settings.json`, `pet.db`, and persistent `service.lock` (file presence does not
mean locked; the OS lock is released on process exit).

Default JSON:

```json
{"schema_version":1,"character_id":"godette","scale":1.0,"screen_id":"","user_name":"你","placement":{"x":1.0,"y":1.0,"edge_x":1,"edge_y":1}}
```

Only `character_id`, `scale`, `screen_id`, `user_name`, `placement` are patchable.
Character IDs are `godette`/`swordsman`; scale is numeric 0.5–2.0, normalized to
float; username is at most 80 Unicode characters; screen ID is a string.
Placement is a complete object with numeric x/y in [0,1] and integer edges in
[-1,1]. Numeric booleans, missing/extra placement keys and API-key fields fail.
The encoded settings document reserves 8 KiB of the 1 MiB frame budget for
response envelopes/warnings. Empty patches are valid.

Placement x is the normalized **foot-center** horizontal position in the work
area; y is the normalized **sole-point** vertical position. Attached edges
override normalized coordinates when the app lays out the character. Saved
scale is independent of display DPI. The service validates/preserves these
values; coordinate/DPI conversion and missing-display fallback belong to the
app, which must preserve the requested `screen_id` preference.

Saves write a sibling temporary file, flush and fsync it, then call os.replace
with at most four attempts (25/50/75 ms delays). A failed save emits no result,
preserves last-successful memory and original config, and permits subsequent
cancel/get/update requests. Successful settings advance only after replacement.
Shutdown has no pending async writes to lose.

Malformed, invalid UTF-8, oversized, invalid-schema or unsupported-version
configuration is copied byte-for-byte to
`settings.json.broken-<UTC timestamp>-<unique UUID>`, fsynced, then replaced with
defaults. Recovery produces a ready warning; the next successful restart has
no recovery warning. Backup or default-save failure produces a bounded startup
`storage_unavailable` error and exit 1, preserving the broken source instead of
pretending recovery succeeded. Read/access failures are not treated as bad JSON.

SQLite schema version/user_version is 1. `settings_metadata(key,value)` records
`settings_schema_version=1` and `database_schema_version=1`; `conversations`,
`messages` (foreign key to conversation), and `memories` start empty. Explicit
BEGIN IMMEDIATE transactions commit or roll back together; foreign keys and
FULL synchronous writes are enabled. Settings JSON is authoritative, not copied
into SQLite. Unsupported database versions fail startup without resetting data.

## Verification

Tests were written before implementation: the initial 35 storage tests failed
because their modules did not exist; the live pipe and package build tests also
failed before their implementations. Deep-nesting and numeric-overflow cases
were observed failing before bounded strict-decoder fixes.

Commands (run from the repository root):

```powershell
& 'D:/phthon lang/python.exe' -B -m pytest tests/service -q -p no:cacheprovider --basetemp tests/service/.tmp-source
& 'D:/phthon lang/python.exe' -B -m pytest tests/service/test_build.py --build-service -q -p no:cacheprovider --basetemp tests/service/.tmp-build
& 'D:/phthon lang/python.exe' -B -m pytest tests/service/test_process.py -q --service-exe 'tests/service/打包验证 中文 空格/service/pet-service.exe' -p no:cacheprovider --basetemp tests/service/.tmp-packaged
```

Results: **74 source/storage tests passed** (the opt-in build test skipped),
**1 actual build/package test passed**, and **39 packaged live-process tests
passed**. Live-process fixtures force `PYTHONIOENCODING=cp936:strict`; packaged
fixtures also remove Python from PATH. The Unicode/emoji round-trip still uses
literal UTF-8 on pipes. Packaged tests run from a copied Chinese path containing
spaces, with Chinese data directories and unrelated working directories.
Source pipe tests invoke the stable `service/main.py` entry with `python -u`,
matching the parent development launcher, and assert silence before hello.

Coverage includes split UTF-8 characters, multiple frames per write, immediate
flushed responses, exact 1 MiB boundary, CRLF, malformed-stream recovery, strict
versions/types, config backups and restart, real failed os.replace with later
recovery, injected fsync failures, bounded replace retries, cancellation,
shutdown ordering, empty/truncated EOF, duplicate-writer exclusion, startup
storage failure, SQLite rollback/commit and bounded protocol-error traffic.
The build test inspects PE subsystem=3 and bundled Python 3.13/SQLite DLLs.

## Remaining integration limits

This service task does not claim Godot UI, hidden-console launch, native Job
Object cleanup, display/DPI behavior, or full-app restart recovery are verified;
those are parent/native-worker acceptance items. No AI, TTS generation, memory
business logic or API credential handling is implemented. Cancellation is an
explicit acknowledgement because this stage has no async AI/TTS jobs.

Crash/power-loss testing during an actual disk flush was not performed; tests
exercise filesystem failures, fsync failures, transaction rollback, clean
restart and last-successful-value preservation. A failed temporary-file cleanup
can leave a `.settings-*.tmp` file without changing the authoritative config.
PyInstaller's warning file contains conditional/platform imports; the packaged
Windows contract suite passes. Runtime traces and build warnings remain under
the assigned service test/build directories.
