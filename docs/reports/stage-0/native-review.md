Native bridge review — 2026-11-01

**Spec-compliance verdict: CONDITIONAL PASS for native API and feature coverage.** The planned interface and mechanisms are present. Cleanup cannot receive unconditional sign-off because of finding 1. Full desktop acceptance remains with the parent: rendered transparency, cross-process clicks, focus, real tray operation, exported Release behavior, and process exit are still unverified.

**Code-quality verdict: REQUEST CHANGES.** One conditional memory-safety defect and one unreliable emergency-exit shortcut need correction. These are static-review findings, not newly reproduced failures.

Review covered `native/src/window_bridge.*`, `native/src/godot_entry.cpp`, `tests/native`, and `docs/reports/stage-0/native.md` against `docs/stage-0-plan.md`. No Git commands or diffs, code changes, test executions, UI automation, or agents were used. This report is the only file written.

**1. [P1] Preserve bridge lifetime until active native callbacks have unwound.**

Location: [window_bridge.cpp:249](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:249), especially the cross-thread `SendMessageW` at line 254; related [show_menu continuation at line 242](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:242) and [godot_entry.cpp:164](D:/danjigame/3D-pet-gtpdo/native/src/godot_entry.cpp:164).

Concrete trigger: the last RefCounted reference is released on a worker while the window thread is inside `show_menu()` / `TrackPopupMenuEx`. `free_instance()` immediately deletes the bridge, whose destructor synchronously sends `shutdown_message`. The menu loop can dispatch that message; `shutdown()` calls `EndMenu()`, destroys the helper, and returns. The worker is then allowed to complete deletion even though the outer `show_menu()` frame has not returned. That frame subsequently writes `menu_open_` and reads other bridge members at lines 242–246, producing a use-after-free race. Stopping the menu does not retain the C++ object or wait for the outer callback to unwind. This follows from the source call graph and the documented [SendMessage completion semantics](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagew); Microsoft's [timed-menu example](https://devblogs.microsoft.com/oldnewthing/20050307-00/?p=36263) also demonstrates callbacks running inside menu tracking.

The same synchronous worker-finalization path can deadlock if the window thread joins that worker without pumping messages: the worker waits for cleanup on the thread waiting for it. The implementation explicitly attempts to support worker final release at lines 251–254, so merely checking `on_thread()` does not make that fallback safe.

Required correction: give callback state an independently retained lifetime, defer its final destruction until active callback/menu frames unwind on the owner thread, and avoid an unbounded synchronous dependency on that thread from the RefCounted destructor. Alternatively, enforce owner-thread finalization through ownership design and remove the purported worker-safe fallback. Replacing `SendMessageW` with `PostMessageW` alone would still leave callbacks pointing at freed storage.

Scope qualification: explicit main-thread shutdown followed by release after callbacks have returned avoids this trigger. No claim is made that the current parent scene releases its bridge on a worker. Existing configured native destructor tests run on one thread; the Godot lifecycle smoke tests use unconfigured objects. Neither exercises this hazard.

**2. [P2] Use a supported key for the emergency exit fallback.**

Location: [window_bridge.cpp:209](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:209), with fallback acceptance at line 217 and advertised behavior in [native.md:94](D:/danjigame/3D-pet-gtpdo/docs/reports/stage-0/native.md:94).

The only exit hotkey is registered with `VK_F12`. Windows explicitly reserves F12 for debuggers and says it should not be registered as a hotkey, even when the application is not being debugged. This makes the advertised emergency route unreliable on affected systems precisely when the tray is unavailable or recovering. The existing failure check is useful, but it does not make F12 an appropriate fallback. See [Microsoft RegisterHotKey documentation](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).

Choose an unreserved chord, keep the registration-success check, and update the tooltip/report consistently. The checks at [window_bridge_test.cpp:106](D:/danjigame/3D-pet-gtpdo/tests/native/window_bridge_test.cpp:106) inject `WM_HOTKEY` directly; they establish dispatch only, not registration or delivery of the physical shortcut. Real shortcut delivery remains a parent acceptance check.

**Requirements and implementation assessment**

| Area | Assessment |
| --- | --- |
| Input API / C ABI | All nine planned methods are registered with the expected types, plus the DPI getter documented as authorized in the native report. Variant calls validate instance, count, and argument type. Ptrcall uses typed storage and `memcpy`; String/StringName temporaries are destroyed. No additional concrete ABI defect found for the supplied x64 engine. This is not a portability verdict for other engine ABIs. |
| Noactivate after configure | `WS_EX_NOACTIVATE`, `MA_NOACTIVATE`, and `PA_NOACTIVATE` are present. The original procedure remains in the subclass chain. Normal bridge operations do not explicitly foreground the role. Initial focus acquired before `configure()` is an application startup/integration responsibility, not a native defect established by this review. |
| Cross-process pass-through | Uses `WS_EX_LAYERED | WS_EX_TRANSPARENT`, in addition to `HTTRANSPARENT`. This matches the documented layered-window hit-test mechanism; it does not rely solely on same-thread hit-test delegation. Actual D3D12 visibility/composition and other-process click delivery require the parent's desktop checks. [Microsoft layered-window behavior](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows). |
| Capture / input | Capture keeps the role interactive; release applies the deferred mode. `WM_CAPTURECHANGED` clears ownership without reacquiring it, `WM_CANCELMODE` releases, and role destruction invalidates capture. Physical cursor/button polling supports the intended integration. `GetCapture()` observes the calling thread only, and background capture has Win32 limits; broader input behavior must not be inferred from hidden same-thread windows. [GetCapture](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getcapture), [SetCapture](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setcapture). |
| Tray / ordinary cleanup | Independent unowned helper, command mapping 1–5, exit queue priority, Explorer notification handler, and retries are implemented. Ordinary same-thread shutdown deletes the tray icon, destroys icon/helper resources, releases hotkeys/timer/capture, removes the subclass/property, and restores only managed style bits. Finding 1 concerns active-callback/finalization lifetime; finding 2 concerns the fallback shortcut. |

**Existing evidence accepted, without rerunning**

- `tests/native/results/win32.log`: records 84 hidden-HWND assertions passing and effective DPI 144 / scale 1.5.
- `godot_api.log` and `godot_release_api.log`: record the ten-method contract, Variant/typed ptrcall smoke, and 50 RefCounted lifecycles per DLL passing. The release file was loaded through an isolated debug-feature mapping, not an exported Release application.
- `summary.json`: records all three passes and explicitly marks real desktop UI as not run. Both test-project DLLs currently match its SHA-256: `b0bf4d3e10d30857aef3476b4caa2f91f898601efb11897fdcc1d708bd42ec0d`.
- `binary-inspection.log`: records x64, the expected entry export, and system-only runtime dependencies.

The report correctly distinguishes synthetic message/style checks from visible desktop acceptance. The existing evidence does not cover either finding above. Suggested follow-up coverage is configured final release during active menu dispatch, owner-thread/worker shutdown ordering, and actual delivery of the replacement exit shortcut; none was run or added during this review.
