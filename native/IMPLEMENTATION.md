# PetWindowBridge implementation plan

Authorized spec: `docs/stage-0-plan.md` and the native worker request.
Work in place; no Git, worktrees, agent spawning, UI automation, or edits outside
`native/`, `prototypes/stage0/addons/pet_window/`, `tests/native/`, and
`docs/reports/stage-0/native.md`.

Architecture: C++ Win32 owner plus a thin engine-generated GDExtension C ABI
adapter exposing a RefCounted `PetWindowBridge`. No godot-cpp, package manager,
or runtime VC redistributable dependency. Hidden independent top-level helper
receives shell broadcasts and owns tray menus; the role HWND never owns a menu.

- [x] Add isolated Godot smoke test for the original nine methods plus the
  authorized `get_dpi_scale() -> float` addition and invalid HWND
  behavior; verify the missing-extension baseline fails explicitly.
- [x] Implement Win32 lifecycle, style changes that preserve composition,
  MA_NOACTIVATE subclassing, physical pointer polling and sticky capture.
- [x] Implement tray menu actions 1–5, Explorer restart handling, retry and
  keyboard fallback, with deterministic shutdown/restoration.
- [x] Bind the ten methods through the local engine ABI and build x64 MSVC
  DLLs in `prototypes/stage0/addons/pet_window/bin` using an offline script.
- [x] Run isolated API smoke and native regression tests for original procedure,
  style and z-order preservation, deferred passthrough during capture, capture
  loss, tray commands, restoration and repeated lifecycle.
- [x] Document integration contract, exact artifacts, observed checks, and
  physical desktop/UI acceptance still assigned to the parent.

Tests use hidden test HWNDs and synthetic messages only. They do not automate
the desktop, activate a visible window, restart Explorer, or change shared
project settings. Export/template and real D3D12 transparency acceptance remain
with the parent.

Native review follow-up:

- [x] Reproduce worker-finalization deadlock in an owner-join regression.
- [x] Separate facade and retained HWND callback state; post worker cleanup and
  defer native resource teardown until outer callbacks/menu frames unwind.
- [x] Replace reserved F12 with Ctrl+Alt+Q and test actual registration/release.
- [x] Check nested owner/worker finalization via a non-UI menu-loop seam, with
  AddressSanitizer, and rerun the original native and both DLL API checks.
