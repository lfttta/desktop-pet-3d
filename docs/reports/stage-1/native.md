Stage 1 native additions — verified 2026-11-01 on Windows x64 / Godot 4.7.2.

Final Godot API additions (fourteen public methods total; the original ten names, argument types and return types are unchanged):

| Signature | Contract |
| --- | --- |
| `acquire_instance(key: String) -> int` | `1` primary, `0` existing instance signaled, `-1` error. Works before or after `configure()`. Same owner/key is idempotent; changing a held key fails. |
| `adopt_service_process(pid: int) -> bool` | Adopts only a live direct child of this process. Re-adoption is idempotent; restarted services use the same job. |
| `terminate_service_processes() -> void` | Calls `TerminateJobObject` on the bridge-owned job, retaining it for subsequent adoption/restart. No job is a no-op. Does not release instance ownership or the activation event. |
| `get_displays_json() -> String` | UTF-8 JSON array of `{id,name,position:[x,y],size:[w,h],work_area:[x,y,w,h],primary:bool}`. Match Godot screen indices by position, not enumeration order. |

Instance names use `Local\\` session scope, the current user's SID and SHA-256 of the exact UTF-8 key. The caller supplies a consistent/canonical data-directory key. The auto-reset activation event is created/opened before the mutex, and is never reset on acquisition, preserving startup notifications. The mutex is a retained, non-thread-owned presence token so worker-thread finalization can close it safely. `poll_tray_action()` consumes activation as `2`; repeated outstanding launches may coalesce. Actions `6` settings and `7` chat join the existing `1..5`; `5` exit retains priority and drains obsolete actions/activation.

Service adoption retains a process handle while checking Toolhelp parent PID and creation time, then assigns it to an unnamed, non-inheritable `KILL_ON_JOB_CLOSE` job. Invalid/self/foreign PIDs and assignment failures return false without terminating those processes. Explicit termination targets job membership, never a newly looked-up PID. Termination requests are asynchronous; parent transport cleanup should close its pipe handles and observe process exit. Successful termination permits later service adoption into the same job. Shutdown/destruction closes the job, event and mutex even without a configured HWND; existing deferred HWND/callback lifetime handling remains in place.

Monitor IDs use `EnumDisplayDevicesW(..., EDD_GET_DEVICE_INTERFACE_NAME)`. A driver that supplies no monitor interface gets an explicit `source:` fallback ID, which is not guaranteed stable. Enumeration temporarily uses per-monitor-v2 thread DPI awareness and restores the caller context. JSON escapes quotes, backslashes and all control characters while retaining Unicode. Enumeration failure returns `[]`.

The C ABI uses the checked-in engine header: String-to-UTF-8 length queries, engine-owned String construction/destruction, separate Variant and ptrcall handling. No godot-cpp dependency or STL object crosses the DLL boundary.

Build and verification:

```text
native\build.cmd --stage1
python tests/native/run_tests.py --stage1
python tests/native/check_build_guard.py
```

The offline MSVC BuildTools build passed `/W4 /WX`. `--stage1` installs both DLL variants and PDB only into `app/addons/pet_window/bin`. No argument, or `--build-only`, compiles without publishing. No Git commands, agents, prototype edits or stage-0 distribution writes were used.

| Evidence | Result |
| --- | --- |
| Before implementation | Original 84 hidden-HWND and 55 lifetime assertions passed; API test failed for missing additions. Saved in `tests/native/results/stage1-baseline-api.log`. |
| Before termination addition | Thirteen-method DLL failed the new fourteen-method API assertion. Saved in `tests/native/results/stage1-terminate-baseline-api.log`. |
| Hidden HWND regression | 90 assertions passed, including actions 6/7 and exit priority over activation. |
| Existing lifetime regression | 55 assertions passed; worker release and nested menu callbacks used the hidden test runner, never a visible popup. |
| Stage 1 native regression | 212 assertions passed: 12 competing processes, startup handoff, owner shutdown/exit, worker destruction, own-child job adoption, foreign rejection, restart, non-inheritance, abrupt owner death, injected event/mutex/job failures with handle-count cleanup, explicit termination/re-adoption, JSON escaping and DPI restoration. |
| Debug and release Godot API | Both passed exact fourteen-method metadata, all original signatures, Variant/typed calls, 50 RefCounted lifecycles, repeated String returns, actual `OS.execute_with_pipe` adoption/termination/restart and Unicode/space cross-process key handoff. |
| Default build guard | All 294 checked files under `prototypes/stage0`, `dist/stage-0` and `app/addons/pet_window` retained content and timestamps. |

Delivered debug/release DLL SHA-256: `7ce5858d3fe2d83e625bde34ee0befa698ce38ab34d9df0cbe75d9ea50cd8d39`. Detailed logs/hashes are in `tests/native/results/summary.json`, `win32.log`, `lifetime.log`, `stage1.log`, `godot_api.log`, `godot_release_api.log` and `build-guard.json`.

Hardware coverage was one 2560×1600 monitor at 144 DPI (work area 2560×1528). Mixed-monitor/hot-plug behavior and visible app UI remain parent integration checks. No actual visible menus or UI automation were run. Python service EOF behavior and end-to-end packaging are outside this native write scope.
