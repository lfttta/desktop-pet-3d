#include "window_bridge.hpp"
#include <cstdio>
#include <cstdlib>

namespace {
int assertions = 0;
void check(bool condition, const char *message) {
    ++assertions;
    if (!condition) {
        std::fprintf(stderr, "FAIL: %s (Win32=%lu)\n", message, GetLastError());
        std::exit(1);
    }
}
LRESULT CALLBACK original_proc(HWND hwnd, UINT message, WPARAM wp, LPARAM lp) {
    if (message == WM_APP + 99) return 12345;
    return DefWindowProcW(hwnd, message, wp, lp);
}
HWND make_window(DWORD ex = WS_EX_TOOLWINDOW) {
    return CreateWindowExW(ex, L"Pet3D.NativeTest", L"hidden native regression",
            WS_POPUP, -450, -350, 260, 200, nullptr, nullptr,
            GetModuleHandleW(nullptr), nullptr);
}
HWND helper = nullptr;
BOOL CALLBACK find_helper(HWND hwnd, LPARAM) {
    wchar_t name[128]{};
    GetClassNameW(hwnd, name, 128);
    if (wcscmp(name, pet3d::WindowBridge::helper_class) == 0) helper = hwnd;
    return TRUE;
}
}

int main() {
    const auto previous_dpi_context = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    check(previous_dpi_context != nullptr, "test thread uses effective per-monitor DPI");
    WNDCLASSW cls{};
    cls.hInstance = GetModuleHandleW(nullptr);
    cls.lpfnWndProc = original_proc;
    cls.lpszClassName = L"Pet3D.NativeTest";
    check(RegisterClassW(&cls) != 0, "test window class");
    const HWND hwnd = make_window();
    const HWND second = make_window();
    check(hwnd && second, "hidden windows created");
    const auto proc = GetWindowLongPtrW(hwnd, GWLP_WNDPROC);
    const auto style = GetWindowLongPtrW(hwnd, GWL_STYLE);
    const auto ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
    RECT before{};
    GetWindowRect(hwnd, &before);
    const HWND foreground = GetForegroundWindow();
    pet3d::WindowBridge bridge;
    check(bridge.get_dpi_scale() == 1.0, "unconfigured DPI fallback is one");
    check(!bridge.configure(0), "zero handle rejected");
    check(!bridge.configure(-1), "invalid handle rejected");
    check(!bridge.configure(reinterpret_cast<int64_t>(GetDesktopWindow())), "foreign HWND rejected");
    check(!bridge.begin_capture(), "unconfigured capture fails");
    check(bridge.configure(reinterpret_cast<int64_t>(hwnd)), "configure valid HWND");
    const UINT effective_dpi = GetDpiForWindow(hwnd);
    check(effective_dpi > 0 && bridge.get_dpi_scale() == static_cast<double>(effective_dpi) / 96.0,
            "DPI scale uses effective HWND DPI, not physical panel DPI");
    std::printf("Effective HWND DPI=%u, scale=%.4f\n", effective_dpi, bridge.get_dpi_scale());
    check(bridge.configure(reinterpret_cast<int64_t>(hwnd)), "configure is idempotent");
    check(!bridge.configure(reinterpret_cast<int64_t>(second)), "configured bridge cannot silently migrate");
    pet3d::WindowBridge duplicate;
    check(!duplicate.configure(reinterpret_cast<int64_t>(hwnd)), "exclusive subclass ownership");
    check(SendMessageW(hwnd, WM_APP + 99, 0, 0) == 12345, "original procedure still called");
    check(SendMessageW(hwnd, WM_MOUSEACTIVATE, 0, 0) == MA_NOACTIVATE, "mouse never activates role");
    check(SendMessageW(hwnd, WM_POINTERACTIVATE, 0, 0) == PA_NOACTIVATE, "pointer never activates role");
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_NOACTIVATE) != 0, "noactivate style");
    bridge.set_interactive(false);
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & (WS_EX_LAYERED | WS_EX_TRANSPARENT)) ==
            (WS_EX_LAYERED | WS_EX_TRANSPARENT), "layered cross-process passthrough bits");
    bridge.set_interactive(true);
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & (WS_EX_LAYERED | WS_EX_TRANSPARENT)) == 0,
            "composition styles restored on interactive");
    check(bridge.begin_capture(), "capture begins");
    bridge.set_interactive(false);
    check(bridge.has_capture(), "capture sticky across hit loss");
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT) == 0,
            "passthrough deferred while captured");
    bridge.end_capture();
    check(!bridge.has_capture() && GetCapture() != hwnd, "explicit release");
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT) != 0,
            "pending passthrough applied on release");
    check(bridge.begin_capture(), "capture reentry");
    SetCapture(second);
    check(!bridge.has_capture() && GetCapture() == second, "capture loss detected without recapture");
    bridge.end_capture();
    check(GetCapture() == second, "release cannot steal another owner's capture");
    ReleaseCapture();
    check(bridge.begin_capture(), "capture before cancel");
    SendMessageW(hwnd, WM_CANCELMODE, 0, 0);
    check(!bridge.has_capture(), "cancel mode releases capture");
    EnumThreadWindows(GetCurrentThreadId(), find_helper, 0);
    check(helper && helper != hwnd, "independent tray helper exists");
    check(GetWindow(helper, GW_OWNER) == nullptr, "role is not tray owner");
    check(!IsWindowVisible(helper), "tray helper hidden outside menus");
    for (int action = 1; action <= 4; ++action) {
        SendMessageW(helper, WM_COMMAND, action, 0);
        check(bridge.poll_tray_action() == action, "tray action mapping");
    }
    for (int action : {6, 7}) {
        SendMessageW(helper, WM_COMMAND, action, 0);
        check(bridge.poll_tray_action() == action, "settings/chat action mapping");
    }
    const auto instance_key = "hidden-window-" + std::to_string(GetCurrentProcessId());
    check(bridge.acquire_instance(instance_key) == 1, "configured instance acquired");
    pet3d::WindowBridge secondary;
    check(secondary.acquire_instance(instance_key) == 0, "configured primary signaled");
    SendMessageW(helper, WM_COMMAND, 6, 0);
    SendMessageW(helper, WM_COMMAND, 7, 0);
    SendMessageW(helper, WM_COMMAND, 5, 0);
    check(bridge.poll_tray_action() == 5, "exit outranks activation and settings/chat");
    check(bridge.poll_tray_action() == 0, "exit drains activation and new actions");
    SendMessageW(helper, WM_COMMAND, 999, 0);
    check(bridge.poll_tray_action() == 0, "unknown commands ignored");
    for (int n = 0; n < 80; ++n) SendMessageW(helper, WM_COMMAND, 1, 0);
    SendMessageW(helper, WM_COMMAND, 5, 0);
    check(bridge.poll_tray_action() == 5, "exit cannot be dropped or delayed by queue overflow");
    check(bridge.poll_tray_action() == 0, "exit drains obsolete actions");
    SendMessageW(helper, WM_HOTKEY, pet3d::WindowBridge::recover_hotkey, 0);
    check(bridge.poll_tray_action() == 2, "recovery hotkey fallback dispatch");
    SendMessageW(helper, WM_HOTKEY, pet3d::WindowBridge::exit_hotkey, 0);
    check(bridge.poll_tray_action() == 5, "exit hotkey fallback dispatch");
    SendMessageW(helper, RegisterWindowMessageW(L"TaskbarCreated"), 0, 0);
    check(IsWindow(helper), "simulated Explorer broadcast retains helper");
    RECT after{};
    GetWindowRect(hwnd, &after);
    check(EqualRect(&before, &after), "negative desktop geometry preserved");
    check(GetWindowLongPtrW(hwnd, GWL_STYLE) == style, "no minimize or normal style changes");
    check((GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST) == (ex & WS_EX_TOPMOST), "topmost not changed");
    check(GetForegroundWindow() == foreground, "bridge never changes foreground during normal use");
    bridge.shutdown();
    bridge.shutdown();
    check(!IsWindow(helper), "helper destroyed");
    check(GetWindowLongPtrW(hwnd, GWLP_WNDPROC) == proc, "exact original WNDPROC restored");
    check(GetWindowLongPtrW(hwnd, GWL_EXSTYLE) == ex, "original extended styles restored");
    check(GetWindowLongPtrW(hwnd, GWL_STYLE) == style, "original normal styles retained");
    check(bridge.poll_tray_action() == 0, "shutdown clears actions");
    for (int iteration = 0; iteration < 10; ++iteration) {
        pet3d::WindowBridge scoped;
        check(scoped.configure(reinterpret_cast<int64_t>(hwnd)), "repeat configure after shutdown");
        scoped.set_interactive(false);
        check(scoped.begin_capture(), "repeat capture");
    }
    check(GetCapture() != hwnd, "destructor releases capture");
    check(GetWindowLongPtrW(hwnd, GWL_EXSTYLE) == ex, "destructor restores styles");
    check(GetWindowLongPtrW(hwnd, GWLP_WNDPROC) == proc, "destructor removes subclass");
    const HWND topmost = make_window(WS_EX_TOOLWINDOW | WS_EX_TOPMOST);
    const auto topmost_ex = GetWindowLongPtrW(topmost, GWL_EXSTYLE);
    check(bridge.configure(reinterpret_cast<int64_t>(topmost)), "configure topmost window");
    bridge.set_interactive(false);
    check((GetWindowLongPtrW(topmost, GWL_EXSTYLE) & WS_EX_TOPMOST) != 0, "passthrough retains topmost");
    bridge.set_interactive(true);
    // An unrelated style set by the application after configure must survive.
    SetWindowLongPtrW(topmost, GWL_EXSTYLE, GetWindowLongPtrW(topmost, GWL_EXSTYLE) | WS_EX_ACCEPTFILES);
    bridge.shutdown();
    check(GetWindowLongPtrW(topmost, GWL_EXSTYLE) == (topmost_ex | WS_EX_ACCEPTFILES),
            "cleanup retains unrelated application style changes and topmost");
    DestroyWindow(topmost);
    const HWND layered = make_window(WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT);
    check(bridge.configure(reinterpret_cast<int64_t>(layered)), "originally layered HWND");
    bridge.set_interactive(true);
    check((GetWindowLongPtrW(layered, GWL_EXSTYLE) & WS_EX_LAYERED) != 0, "original layered bit retained");
    bridge.shutdown();
    check((GetWindowLongPtrW(layered, GWL_EXSTYLE) & (WS_EX_LAYERED | WS_EX_TRANSPARENT)) ==
            (WS_EX_LAYERED | WS_EX_TRANSPARENT), "original transparent bit restored");
    DestroyWindow(layered);
    check(bridge.configure(reinterpret_cast<int64_t>(hwnd)), "configure before external destruction");
    DestroyWindow(hwnd);
    check(!bridge.has_capture() && !bridge.begin_capture(), "destroyed role cannot capture");
    check(bridge.get_dpi_scale() == 1.0, "destroyed HWND DPI fallback");
    check(bridge.poll_tray_action() == 5, "unexpected role destruction requests exit");
    bridge.shutdown();
    DestroyWindow(second);
    UnregisterClassW(cls.lpszClassName, cls.hInstance);
    SetThreadDpiAwarenessContext(previous_dpi_context);
    std::printf("PASS: %d hidden-HWND native assertions\n", assertions);
    return 0;
}
