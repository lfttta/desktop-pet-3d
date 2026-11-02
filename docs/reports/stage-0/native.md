# Stage 0 native bridge report

Date: 2026-11-01. Native worker implementation and isolated tests complete;
real desktop acceptance remains assigned to the parent. No Git commands,
commits, worktrees, agent spawning, or UI automation were used. Shared
`prototypes/stage0/project.godot` was not edited.

## Final handoff after native review fixes

P1 and P2 fixes are implemented and ready for independent source re-review;
this report does not claim reviewer sign-off. The final delivered debug/release
DLLs were rehashed against `tests/native/results/summary.json` and both match:

```text
SHA-256 0e26761393a6350835b410e0fefe17d3c1b76f052cdcc96e31b06e1afe3a667a
```

Saved verification for this build: 84 existing native assertions, 55 new
lifetime/hotkey assertions, the same lifetime cases under AddressSanitizer,
and both debug/release ten-method Godot API smoke tests passed. This final
report-only pass inspected those logs and hashes; it did not rerun tests, send
application input, open a menu, or launch a visible application. UI testing is
stopped for this turn. The parent's next step is package rebuild and exported
headless verification; that exported result is not included in these passes.

Reviewer entry points: `native/src/window_state.hpp` for independent ownership;
`native/src/window_bridge.cpp:23` for facade finalization, `:63` for callback
retention/depth, `:324` for nonblocking cleanup requests, and `:340` for deferred
owner-thread teardown. Replacement hotkey registration is at `:273`.
`tests/native/lifetime_test.cpp` covers the owner-join and nested-menu cases.
The parent API, `no_focus=true`, and immediate `set_interactive(false)` setup
remain unchanged by these fixes.

## Delivered

- `native/src/window_bridge.{hpp,cpp}`, `window_state.hpp`: Win32 lifecycle, noactivate subclass,
  physical cursor/button polling, capture, tray commands, effective HWND DPI.
- `native/src/godot_entry.cpp`: C++ GDExtension C ABI adapter registering
  `PetWindowBridge : RefCounted`; original nine methods plus authorized
  `get_dpi_scale() -> float`.
- `native/build.cmd`: offline x64 VS 2022 BuildTools build; `/W4 /WX /O2 /MT`,
  PDB symbols, reproducibility flags. No godot-cpp dependency.
- `native/refresh_abi.py`, `native/vendor/godot/`: engine-generated ABI header,
  version/size/hash metadata, Godot MIT license. Supplied engine identifies as
  `4.7.2.stable.official.ed1daf0bf`.
- `prototypes/stage0/addons/pet_window/pet_window.gdextension` and
  `bin/pet_window.windows.debug.x86_64.dll`,
  `bin/pet_window.windows.release.x86_64.dll`, `bin/pet_window.pdb`.
  The two DLLs intentionally contain the same optimized release binary and use
  the static CRT. MIT notice is also delivered in `GODOT_LICENSE.txt`.
- `tests/native/`: hidden-HWND regression executable source, private Godot test
  project, runner, and result logs. `native/README.md` contains integration details.

## Verified on this machine

| Check | Result |
| --- | --- |
| MSVC x64 build, warnings treated as errors | PASS |
| Hidden Win32 windows, 84 assertions | PASS |
| Worker finalization, nested menu teardown and hotkey registration, 55 assertions | PASS |
| Same 55 lifetime/hotkey assertions with MSVC AddressSanitizer | PASS, no sanitizer findings |
| Original Godot-style procedure delegation/restoration | PASS using custom original procedure |
| Invalid, foreign, duplicate HWND and silent migration rejection | PASS |
| `WS_EX_NOACTIVATE`, mouse/pointer noactivate messages | PASS |
| Layered/transparent bits, interactive restoration | PASS |
| Capture remains interactive across `set_interactive(false)` | PASS |
| Explicit release, ownership loss, cancel, destructor cleanup | PASS |
| Role geometry at negative desktop coordinates, normal styles, topmost | PASS |
| Unrelated application style changes survive cleanup | PASS |
| Tray command 1–5 dispatch, exit priority/overflow, fallback dispatch | PASS via synthetic messages to helper |
| TaskbarCreated re-add handler | PASS via synthetic broadcast message; Explorer was not restarted |
| Hidden independent/unowned tray helper and its destruction | PASS |
| Foreground unchanged during ordinary non-menu operations | PASS |
| Effective HWND DPI getter | PASS, per-monitor-aware test HWND returned **144 / 96 = 1.5** |
| Actual debug DLL load in supplied Godot | PASS |
| Actual release DLL load in supplied Godot | PASS via isolated test mapping |
| Exact ten methods, argument/return types, Variant and typed ptrcall | PASS |
| 50 RefCounted creation/cleanup lifecycles per DLL | PASS, no engine error/leak messages |
| PE machine and exports | x64, `pet_window_library_init` |
| Non-system runtime dependency | None: User32, Shell32, Comctl32, Kernel32 only |

Commands:

```powershell
& .\native\build.cmd
& 'D:\phthon lang\python.exe' .\tests\native\run_tests.py
& .\native\check_lifetime_asan.cmd
```

Evidence: `tests/native/results/win32.log`, `lifetime.log`, `lifetime-asan.log`, `godot_api.log`,
`godot_release_api.log`, `summary.json`, `binary-inspection.log`.
`summary.json` contains the current delivered DLL SHA-256 hashes.

The initial no-extension smoke failed with `PetWindowBridge must load`. The
later DPI addition was first exercised against the previous nine-method DLL;
that failed and exposed a smoke-test early-exit issue, now corrected. The final
test runner terminates on contract mismatch and records/kills timed-out children.

## Integration caveats

Call `configure` on Godot's main/window thread after final transparency/window
flag setup, using `DisplayServer.window_get_native_handle(DisplayServer.WINDOW_HANDLE,
window_id)`. Success starts interactive. All lifecycle/mode/capture/poll calls
except shutdown belong to the same thread. Keep the RefCounted instance alive;
explicitly `shutdown()` on the owner before stopping its event loop. Reconfigure
only after shutdown if the HWND changes. The parent sets `no_focus=true` and
immediately `set_interactive(false)` after configure to prevent startup blank
space from intercepting clicks; native API defaults remain unchanged.

Get effective scale from `bridge.get_dpi_scale()`, not screen physical DPI.
It queries `GetDpiForWindow(hwnd)` every time, returns `1.0` without a valid
configured window, and depends on that window's DPI-awareness context.
The independent current test measured 1.5; the earlier 125% report is not a
basis for forcing 1.25. Query the actual role HWND. Physical cursor coordinates
already use desktop pixels; convert logical layout dimensions only once.

Background-window capture is limited by Windows. Poll cursor and physical left
button while dragging; on release/capture loss/hide call `end_capture()`. A false
interaction request is deferred until capture ends. The bridge reports genuine
native capture loss and never steals capture back. Godot may release capture
on mouse-up.

Continue polling actions while hidden/paused. Actions: 0 none, 1 show/hide,
2 recover position, 3 next character, 4 idle/wave, 5 exit. Chinese/English popup
menu focus belongs to an independent helper, never the role. Escape routes are
Ctrl+Alt+F10 (recover) and Ctrl+Alt+Q (exit), subject to hotkey conflicts; setup
logs conflicts and refuses success if both the tray and exit fallback fail.
Exit has queue priority; native menu tracking ends when exit is requested.

Pass-through adds `WS_EX_LAYERED | WS_EX_TRANSPARENT`. Interactive mode restores
the originally recorded layered bit. No SetLayeredWindowAttributes,
UpdateLayeredWindow, role ShowWindow/SetWindowPos, minimize, z-order, or alpha
override is performed. The original Godot procedure remains in the subclass
chain, and cleanup restores only the owned extended-style bits.

## Native review corrections

The findings in `native-review.md` are addressed without changing the ten-method
Godot API. The review file itself was left unchanged.

**P1: asynchronous finalization and retained callback state.** The Godot-facing
`WindowBridge` now owns a shared `WindowState`. Configured HWNDs retain that
state independently, and native userdata never points at the facade. Worker
shutdown/final release sets an atomic request and posts a wake; it never calls
`SendMessageW` or waits for the owner. Every role/helper callback retains the
state until its complete stack frame, including delegated Godot procedure and
menu continuation, has returned. A callback-depth guard defers native cleanup
until the outermost callback unwinds. Nested shutdown ends menu tracking;
helper/subclass/style/capture teardown then runs on the owner thread. The DLL
is pinned once configured, matching its existing non-reloadable contract, so
deferred callbacks also retain valid executable code.

The owner must resume pumping after worker finalization. A failed wake post is
retried implicitly by the next callback/timer checking the atomic request. If
the owner never pumps again, native state is intentionally retained until
process termination, avoiding stale userdata; normal shutdown should occur on
the owner before that event loop stops. Concurrent ordinary facade use and
deletion remains unsupported.

**P2: supported exit shortcut.** Ctrl+Alt+Q replaces the reserved debugger F12
key everywhere in production code, tooltip and current instructions. The
registration-success/failure checks remain. The new regression verifies actual
RegisterHotKey ownership and subsequent release of Ctrl+Alt+Q; physical keyboard
delivery is still the parent's acceptance test.

New regression evidence:

- `lifetime-baseline.log` records the original build failing to finish worker
  deletion while the owner joined without pumping, and failing Ctrl+Alt+Q
  registration ownership.
- `lifetime.log` now passes 55 assertions: owner-join ordering, restored HWND
  resources, owner/worker final release inside nested menu dispatch, with/without
  prior reentrant explicit shutdown, state retention during the outer frame,
  state reclamation after it returns, and hotkey registration/release.
- A compile-time test seam replaces only menu tracking/display with nested
  message dispatch. It exercises the production menu continuation and callback
  guards without UI automation or focus changes. The delivered DLL contains
  neither the seam nor test lifetime counters.
- `lifetime-asan.log` passes the same cases with `/fsanitize=address`.
- The full build and 84 existing Win32 assertions, debug/release ten-method API
  smoke, typed calls and 50 RefCounted lifecycles per binary were rerun after
  the fix. Both final DLL paths have been updated; hashes are in `summary.json`.

## Parent desktop acceptance still needed

Native tests prove state transitions and message dispatch, not rendered desktop
behavior. Parent reported Mobile + D3D12 rendering and 14 input tests passing,
then real desktop transparency, drag start/end logs, wave clicks without rendering
loss, and exit of the previous process through its old shortcut. These are
parent-provided results, not this worker's UI checks. Foreground HWND and
cross-process click-through measurements remain with the parent on the final DLL.

- Visible D3D12 per-pixel alpha while repeatedly switching pass-through.
- Cross-process pass-through over desktop, Explorer, Notepad and browser;
  clicking/dragging the role never changes keyboard focus.
- Actual tray popup selection, dismissal, all five commands, fallback shortcuts,
  hidden-role recovery, and exit with no remaining process/icon.
- Actual Explorer restart/recovery (only synthetic handler invocation tested).
- Drag outside role bounds, release/capture loss while another app is active.
- Current role-window DPI and additional scales/monitors/negative-monitor paths.
- Independent exported Release application and Chinese/space launch paths.

References: [Microsoft layered windows](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows),
[SetCapture](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setcapture),
[GetDpiForWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow),
[TrackPopupMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu).
