Native fix re-review — 2026-11-01

**Verdict: GO for parent packaging from this scoped native review.** P1 and P2 are ADDRESSED. No remaining important defect or new blocking regression was identified in the native fix. This verdict does not cover the separately owned application fixes or desktop acceptance.

**Spec-compliance: PASS for the two requested corrections. Code-quality: PASS for the native fix within its documented lifecycle contract.**

| Original finding | Status | Source basis |
| --- | --- | --- |
| P1: worker finalization could free active menu callback state or deadlock on the owner | **ADDRESSED** | Independently retained `WindowState`, callback-depth guards, and asynchronous shutdown requests replace facade-backed callbacks and synchronous worker cleanup. |
| P2: emergency exit used reserved F12 | **ADDRESSED** | Registration, tooltip, and production warnings now use Ctrl+Alt+Q; registration failure handling remains. |

**P1 evidence and regression assessment**

- [window_state.hpp:54](D:/danjigame/3D-pet-gtpdo/native/src/window_state.hpp:54) separates atomic owner/wake/request fields from owner-thread state and adds the configured lifetime reference and callback depth.
- [window_bridge.cpp:23](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:23) releases the facade independently. Configuration retains state at line 92; native callbacks point to `WindowState` and acquire their own strong reference through `CallbackScope` at line 63.
- [window_bridge.cpp:324](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:324) makes worker shutdown nonblocking. The request is atomic and the wake is posted; there is no synchronous wait on the owner. Callback guards cover both role and helper procedures, including delegated procedure execution and the complete menu continuation.
- [window_bridge.cpp:67](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:67) defers teardown until callback depth reaches zero. Cleanup at line 340 removes native resources before releasing configured ownership at line 376. The outer callback's strong reference survives that release. The `stopping_` guard prevents cleanup recursion during destruction messages.
- [lifetime_test.cpp:87](D:/danjigame/3D-pet-gtpdo/tests/native/lifetime_test.cpp:87) exercises worker deletion while the owner joins without pumping, then verifies cleanup after pumping. The nested-menu cases at line 102 cover owner/worker deletion, with and without prior explicit shutdown, and assert that state/helper/subclass survive the active outer frame and are reclaimed afterward.

The new allocation in the facade constructor is accounted for: [godot_entry.cpp:148](D:/danjigame/3D-pet-gtpdo/native/src/godot_entry.cpp:148) catches `std::bad_alloc` and returns null. The ten-method API remains intact. The production build excludes `PET_WINDOW_TESTING`; the menu seam and state counters are restricted to the regression build. DLL pinning is explicit and consistent with the existing non-reloadable addon contract.

**P2 evidence**

[window_bridge.cpp:273](D:/danjigame/3D-pet-gtpdo/native/src/window_bridge.cpp:273) registers Ctrl+Alt+Q, with matching tooltip at line 229 and diagnostics at lines 277–288. [lifetime_test.cpp:126](D:/danjigame/3D-pet-gtpdo/tests/native/lifetime_test.cpp:126) verifies actual registration ownership and release, extending the earlier synthetic `WM_HOTKEY` dispatch coverage. Physical key delivery is still a desktop acceptance item.

**Existing verification accepted; no tests rerun**

- `lifetime-baseline.log`: records the former owner-join failure and absence of Ctrl+Alt+Q ownership. This baseline does not establish an ASan reproduction of the former use-after-free.
- `lifetime.log`: 55 lifetime/hotkey assertions, zero failures.
- `lifetime-asan.log`: the same 55 assertions pass under the recorded AddressSanitizer build, with no sanitizer diagnostic in the saved log.
- `win32.log`: 84 existing native assertions pass.
- `godot_api.log` and `godot_release_api.log`: both record the ten-method Variant/typed-call checks and 50 RefCounted lifecycles passing.
- `summary.json`: native, lifetime, and both API checks are marked pass. Fresh read-only SHA-256 checks confirm both test-project DLLs and both delivered native addon DLLs match `0e26761393a6350835b410e0fefe17d3c1b76f052cdcc96e31b06e1afe3a667a`.

**Remaining important issue: none identified within this scope.** One documented lifecycle limitation remains: worker finalization requests cleanup, so the owner must resume message processing. If it never pumps again, configured native state is retained until process termination. Normal shutdown should run on the owner before its event loop stops; concurrent ordinary facade use and deletion remains unsupported. This is explicit in [native.md:153](D:/danjigame/3D-pet-gtpdo/docs/reports/stage-0/native.md:153) and does not reopen the original deadlock/use-after-free finding. The nested-menu regression uses a deterministic dispatch seam, so these passes do not establish real popup behavior or exported-application acceptance.

Reviewed current native sources, lifetime tests/logs, relevant build flags, and native DLL identity. Application sources were not reviewed. No Git, UI automation, agents, builds, test runs, or code edits were performed. This re-review report is the only file written.
