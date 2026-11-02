Stage 0 app/build bounded fix round — 2026-11-01

**DONE_WITH_CONCERNS: all seven app-review findings are implemented and the final headless checks pass.** Real desktop/GUI acceptance remains pending. No Git, worktrees, agents, UI automation or visible application launches were used. Native code, asset sources, baked animation caches and body fitting were not edited.

**Finalized handoff: source frozen at the versions used by the passing isolated build.** This finalization changed only this report; it did not rerun tests or builds. The parent owns the final default-destination build and the ongoing scoped re-review. This report does not claim that re-review is complete.

| Finding | Status | Implemented behavior | Location |
|---|---|---|---|
| 1. Initial passthrough | Fixed; startup order checked headlessly, live HWND acceptance pending | Configure the native bridge and immediately set passthrough before world/model loading; preserve creation-time no-focus. Prime the held-button state after loading so a desktop drag begun during loading is not taken over. | [main.gd:30](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/scripts/main.gd:30) |
| 2. Hidden replacement / facial options | Fixed; regression passed | Disable candidates while loading; apply hidden processing state and facial options to every adopted actor. Hide/switch/show and the Godette option round trip retain the state. | [main.gd:160](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/scripts/main.gd:160) |
| 3. Quick-drag release | Fixed; regression passed | Evaluate displacement before mouse-up classification and apply the final grab-offset position. Capture loss while still held remains cancellation. | [drag_state.gd:14](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/scripts/drag_state.gd:14) |
| 4. Cancellation FPS | Fixed; regression passed | Every cancellation restores 30 FPS when visible or 5 FPS when hidden. | [main.gd:312](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/scripts/main.gd:312) |
| 5. Greeting completion / parent state | Fixed; real playback regression passed on both characters | Duplicate the runtime library and wave resource, set only the duplicate wave to `LOOP_NONE`, and synchronize the parent through `action_changed`. | [character_view.gd:73](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/scripts/character_view.gd:73) |
| 6. Real post-export verification | Fixed; both generated-package paths passed | Run the generated Release EXE against its own adjacent PCK/DLLs, then one fresh copy under a Chinese/space path. Missing PCK, nonzero exit, engine/script errors or missing success marker prevent manifest generation and success reporting. | [build_stage0.py:71](D:/danjigame/3D-pet-gtpdo/tools/build_stage0.py:71), [verify_stage0_package.py:16](D:/danjigame/3D-pet-gtpdo/tools/verify_stage0_package.py:16) |
| 7. Resource-aware exclusion guard | Fixed; positive and negative export fixtures passed | Detect raw files, `.remap`, `.import` and resource-loader paths. Exercise that same production guard in a separate minimal Release export with intentionally included raw JSON, compiled script and imported image. | [verify_export.gd:9](D:/danjigame/3D-pet-gtpdo/tests/verify_export.gd:9) |

The supplied wave is **0.53333336 seconds** on both characters. Runtime greetings deliberately play that existing clip once, then idle; the source library remains `LOOP_LINEAR`. The parent regression waits for real playback completion rather than emitting a completion signal manually, then checks that one tray action starts the next greeting. No animation duration, pose or source resource was rewritten.

Headless hide testing also exposed an existing engine error: Godot does not allow changing the main Window's visibility. The handler now hides the Node3D contents and diagnostic label, disables actor processing and forces native passthrough while leaving the transparent HWND alive. Restoring shows the contents and resumes processing. This provides the intended visible behavior without adding a native API; physical desktop behavior still needs the deferred GUI checks.

The installed Release template omits `--script` and rejects `--path` overrides. Consequently, the original Python verifier command could not actually run its external script. The generated app now has an explicit `--headless -- --verify-package=<absolute script path>` entry, dispatched after `_ready` completes. It loads the single authoritative external verification script while all `res://` model/runtime resources resolve from the generated EXE's PCK. The verifier also rejects editor/non-release execution. The Python helper is import-safe and shares this one implementation between build integration and standalone verification; no permanent duplicate package is kept for the Chinese-path check.

The additional packaging requirement is included at [build_stage0.py:70](D:/danjigame/3D-pet-gtpdo/tools/build_stage0.py:70): copy the model attribution text to `licenses/assets-ATTRIBUTION.txt`. In the tested package it is byte-identical to the source, and `THIRD_PARTY_NOTICES.md` is byte-identical to the parent's updated root notice. All 10 manifest entries were checked for matching sizes and SHA-256 hashes.

**Failure-before / final-pass evidence** is saved under [app-fixes test results](D:/danjigame/3D-pet-gtpdo/prototypes/stage0/tests/results/app-fixes).

| Check | Before | Final |
|---|---|---|
| Interaction regression | `interaction-red.log`: 18 checks, 2 failures for quick drag/click and final position. | `final-build/interaction-tests.log`: 18 checks, 0 failures. |
| Parent lifecycle | `parent-red.log`: 29 checks, 21 failures covering startup ordering, hidden replacement, facial options, FPS and actual looping-wave behavior; also records the main-window visibility error. | `final-build/parent-tests.log`: 31 checks, 0 failures, including the two added content-visibility assertions. Both source loop modes remain 1 and runtime wave modes are 0. |
| Existing character regression | Existing forehead/facial/bone-length coverage retained. | `final-build/character-tests.log`: 12 checks, 0 failures. |
| Existing secondary motion | Existing warmup/translation test retained. | `final-build/secondary-tests.log`: observed rotation `0.03423015028238`, above the test's `0.001` threshold. |
| Build must reject missing PCK | `build-pipeline-red.log`: failed because the old build certified an editor-export success without a PCK. | `build-pipeline-green.log`: 1 test passes; real post-export validation rejects the incomplete package before a manifest is written. |
| Exported guard fixture | `package-guard-red/guard.log`: guard contract assertion failed before the resource-aware guard existed. | `package-guard-green/guard.log`: 9 checks, 0 failures; raw script/image source paths are absent but remap/import resources exist. |
| Deliberately included forbidden files | Separate temporary fixture export, independent of the app package. | `package-guard-green/negative.log`: 3 fixtures rejected, expected exit 1, no check failures. |
| Generated Release entry | `export-entry-red.log`: immediate dispatch during `_ready` produced engine errors despite a zero failure summary; the strict Python gate correctly rejected it. Dispatch is now deferred. | `final-build/export-package-tests.log` and `export-chinese-path-tests.log`: actual generated EXE paths recorded, `EXPORT_TEST failures=0`, exit 0 and no engine/script errors. |

The final complete build command ran import, all four GDScript suites, Release export, normal-path verification, Chinese/space-path verification and manifest generation successfully. Python source parsing and package attribution/manifest checks also passed. Fixture export, import and execution were all headless; timed-out setup attempts were terminated by their subprocess timeout handlers.

Reproduction from PowerShell:

```powershell
& 'D:/phthon lang/python.exe' -B 'D:/danjigame/3D-pet-gtpdo/tools/build_stage0.py' --output 'D:/danjigame/3D-pet-gtpdo/tests/package_guard/build-output' --report-dir 'D:/danjigame/3D-pet-gtpdo/prototypes/stage0/tests/results/app-fixes/final-build'
& 'D:/phthon lang/python.exe' -B 'D:/danjigame/3D-pet-gtpdo/tests/package_guard/test_build_pipeline.py'
& 'D:/phthon lang/python.exe' -B 'D:/danjigame/3D-pet-gtpdo/tests/package_guard/run_guard_tests.py'
```

The validated package is [tests/package_guard/build-output](D:/danjigame/3D-pet-gtpdo/tests/package_guard/build-output). Its PCK SHA-256 is `2c026f5e38fa6613686241d8e1d7dd204f3f7910936d913891cbaec619888724`. This round used the new `--output`/`--report-dir` overrides to keep validation artifacts inside the authorized test scope. The parent's final `dist/stage-0` package still needs its normal build after final asset/document integration; omitting those overrides uses the original default destination and includes the new attribution copy and post-export checks.

Remaining concerns are the already-pending real desktop checks: cold-start passthrough/no-focus, physical tray interactions, hidden visual restoration, DPI/multiple monitors and performance. Startup ordering is covered with a substituted native boundary in the headless parent test, not claimed as a live HWND test. No Phase 1 work is included.

Frozen source SHA-256 prefixes, rechecked during report finalization: `main.gd=1380705cf94a`, `character_view.gd=e0eebf7a5fe7`, `drag_state.gd=4f13261deccd`, `build_stage0.py=f00dcf575dc3`, `verify_stage0_package.py=0d91ea7355df`, `verify_export.gd=2ea9b2726f58`.
