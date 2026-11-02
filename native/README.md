# PetWindowBridge (Godot 4.7.2 / Windows x64)

Small C++ GDExtension backed by the ABI header generated from the supplied
`Godot_v4.7.2-stable_win64.exe` (`4.7.2.stable.official.ed1daf0bf`). No godot-cpp,
SCons, downloads, or package manager are required to build. The original nine
methods are unchanged; `get_dpi_scale()` is the authorized integration addition.

## Build and check

From `D:\danjigame\3D-pet-gtpdo`, in PowerShell:

```powershell
& .\native\build.cmd
& 'D:\phthon lang\python.exe' .\tests\native\run_tests.py
& .\native\check_lifetime_asan.cmd
```

`build.cmd` defaults to VS 2022 BuildTools at
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`.
Set `VS_BUILDTOOLS` to override that installation directory. It invokes the x64
MSVC toolchain with C++17, `/W4 /WX /O2 /MT /Zi /Brepro`, and links User32,
Shell32 and Comctl32. All intermediate files stay under `native/build/`.
Use `build.cmd --build-only` to stage a build there while the delivered DLL is
in use. A normal build copies the binaries after their consumers have exited.
The separate ASan command builds/runs only the hidden lifetime regression with
AddressSanitizer; it does not replace delivered DLLs.

Delivered files under `prototypes/stage0/addons/pet_window/`:

- `pet_window.gdextension`
- `bin/pet_window.windows.debug.x86_64.dll`
- `bin/pet_window.windows.release.x86_64.dll`
- `bin/pet_window.pdb`
- `GODOT_LICENSE.txt`

Debug and release names intentionally contain the same optimized, static-CRT
binary. The matching PDB provides debugging symbols. There is no debug-CRT or
VC redistributable installation requirement. `reloadable=false` disables hot
reload; first configuration also pins the DLL for the process lifetime so
deferred native callbacks cannot run unloaded code. Restart any process using the
extension before replacing its DLL. Export the addon with the project; the
`.gdextension` maps both Windows x64 configurations.

To regenerate the vendored header from the same engine (optional, not part of
normal builds):

```powershell
& 'D:\phthon lang\python.exe' .\native\refresh_abi.py
```

Only `vendor/godot/gdextension_interface.h`, its MIT license, and small ABI
metadata are needed as source dependencies. The full generated API dump stays
under `native/build/abi/`. The script checks engine version, used builtin sizes,
and the `Object.notification` hash before replacing vendor data.

## Public API

The registered Godot class `PetWindowBridge` inherits `RefCounted`.

| Method | Behavior |
| --- | --- |
| `configure(hwnd: int) -> bool` | Accepts a live top-level HWND belonging to this process and calling thread. Idempotent for the same HWND; rejects another bridge's HWND and silent migration to another window. Starts interactive. Rolls back on setup failure. |
| `set_interactive(interactive: bool)` | Clears pass-through when true. When false, enables `WS_EX_LAYERED | WS_EX_TRANSPARENT`; defers this while captured. Never releases capture itself. |
| `get_cursor_position() -> Vector2i` | `GetPhysicalCursorPos`, signed physical desktop pixels. Returns `(0,0)` if Windows cannot read the cursor. |
| `get_dpi_scale() -> float` | Current `GetDpiForWindow(hwnd) / 96.0`, queried each call. Returns `1.0` if unconfigured/destroyed or Windows reports zero. No physical-panel-DPI estimate or cache. |
| `is_left_button_down() -> bool` | Physical left button via high bit of `GetAsyncKeyState(VK_LBUTTON)`. |
| `begin_capture() -> bool` | Forces interaction while capturing; checks `GetCapture()` for success. Refuses to steal capture from a different window on this thread. |
| `end_capture()` | Releases only this role's capture and applies any pending interaction state. |
| `has_capture() -> bool` | Actual capture ownership; false after `WM_CAPTURECHANGED`/cancel or role destruction. Does not reacquire on capture loss. |
| `poll_tray_action() -> int` | Returns one queued command or zero. Exit has priority and clears obsolete actions. |
| `shutdown()` | Idempotent cleanup request. Immediate outside owner callbacks; deferred until the outer owner callback unwinds, or asynchronously posted when called from a worker. Releases capture, icon, hotkeys, timer/helper, styles and subclass. Also called by destructor. |

## Integration contract

Configure on the main/window thread after establishing the final transparent,
borderless Godot window flags. Keep the instance alive until normal shutdown:

```gdscript
var bridge: PetWindowBridge = PetWindowBridge.new()

func _ready() -> void:
    var hwnd := DisplayServer.window_get_native_handle(
        DisplayServer.WINDOW_HANDLE, get_window().get_window_id())
    if not bridge.configure(hwnd):
        push_error("PetWindowBridge configuration failed")
        get_tree().quit(1)
        return
    bridge.set_interactive(false) # startup blank space passes through immediately
    var effective_scale := bridge.get_dpi_scale()
    print("Effective HWND scale: ", effective_scale)

func _exit_tree() -> void:
    bridge.shutdown()
```

Call configure/mode/capture/poll methods on the same main/window thread.
`shutdown()` and final release may happen on a worker: they set an atomic request
and use `PostMessageW`, with no synchronous wait on the owner. HWND callbacks refer
to independently retained `WindowState`, never the Godot facade. Every callback
holds a strong reference and contributes to an owner-thread depth counter; nested
shutdown ends menu tracking and waits for the outer callback to unwind before
tearing down HWND resources and releasing their retained state.

Explicitly call `shutdown()` on the main thread before stopping its event loop.
After worker finalization the owner must resume pumping messages for cleanup;
if the wake cannot be posted, its next callback/timer observes the same atomic
request. If the owner never resumes, native state is deliberately retained until
process termination rather than freed while HWNDs still reference it. Ordinary
API use concurrent with facade deletion is unsupported. These lifetime changes
do not change any Godot method names or return types.

The parent sets `no_focus=true` at startup and immediately calls
`set_interactive(false)` after `configure`; keep both integration choices.

Keep polling commands even while the role is hidden or game logic is paused
(for example, a controller using `PROCESS_MODE_ALWAYS`). Godot's Windows event
loop dispatches the helper's tray and hotkey messages. `poll_tray_action()` does
not pump arbitrary Win32 messages itself.

| Action | Meaning / suggested controller behavior |
| --- | --- |
| 0 | No command. |
| 1 | Toggle visibility; release capture before hiding. |
| 2 | Recover position on a visible monitor and show without activation. |
| 3 | Next character. |
| 4 | Toggle idle/wave. |
| 5 | Call `shutdown()` and quit the tree. |

Left-clicking the tray queues action 1. Right-click opens a Chinese/English
native menu with all five actions. `Ctrl+Alt+F10` queues recovery and
`Ctrl+Alt+Q` queues exit when Windows grants these hotkeys; conflicts log a
message. If icon creation initially fails, setup succeeds only when the exit
hotkey is available, and retries the icon every two seconds. The hidden
top-level helper receives `TaskbarCreated` and re-adds the icon. A standard
Windows application icon is copied at runtime and destroyed during cleanup.

Role HWND rules:

- `WS_EX_NOACTIVATE`, `MA_NOACTIVATE`, and `PA_NOACTIVATE` prevent mouse/pointer
  activation. Original Godot messages go through `DefSubclassProc`; shutdown
  removes only this subclass.
- Native popup focus belongs exclusively to a separate, unowned offscreen
  helper. It is briefly shown without activation and then used as the menu's
  foreground owner; the role is never passed to `SetForegroundWindow`.
- No role `ShowWindow`, `SetWindowPos`, normal-style, alpha/color-key, or renderer
  calls occur. Cleanup restores only the three owned extended-style bits and
  retains unrelated live application state (visibility, topmost, etc.).
- Avoid setting Godot mouse-pass-through or resetting window style flags after
  configuration; those can overwrite these native flags. Use this bridge as the
  interaction-mode owner. A recreated HWND requires shutdown and configure.

Capture stays interactive across projected-body hit loss. The controller must
still poll physical cursor/button state for dragging outside an inactive role
window: Win32 limits mouse-message delivery for background capture. On physical
release, hiding, or capture loss, finish/cancel the drag and call `end_capture()`.
The engine may release native capture on mouse-up; `has_capture()` reports that
loss rather than pretending capture is still held.

`get_dpi_scale()` follows the configured HWND's effective DPI awareness. Do not
substitute `DisplayServer.screen_get_dpi()/96`; do not multiply physical cursor
or native window coordinates a second time. Apply the scale once when converting
chosen logical layout dimensions to physical dimensions. Requery after monitor
changes; per-monitor behavior depends on Godot's DPI-aware window setup.

## Evidence and limits

`tests/native/run_tests.py` creates a private addon copy in `tests/native/godot`,
then runs the hidden-HWND executable and headless API checks against both DLL
paths. It never imports or changes `prototypes/stage0/project.godot`.
Logs, DLL hashes and the summary are under `tests/native/results/`.
The lifetime regression uses a test-only menu-loop seam to dispatch nested
shutdown during the real `show_menu` continuation, without displaying a menu or
changing focus. It verifies owner/worker finalization, native-state reclamation,
and actual Ctrl+Alt+Q registration/release. The seam and counters are absent from
the delivered DLL. Physical shortcut delivery remains a parent UI check.

See `docs/reports/stage-0/native.md` for measured results and outstanding real
desktop acceptance. No UI automation was performed by this worker.

Platform references: [layered-window hit testing](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows),
[capture limits](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setcapture),
[effective HWND DPI](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow),
[popup menu ownership](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu).
