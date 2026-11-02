#include "window_bridge.hpp"
#include "window_test_hooks.hpp"
#include <cstdio>
#include <cstdlib>

namespace {
int failures = 0;
int assertions = 0;
void check(bool condition, const char *message) {
    ++assertions;
    if (!condition) { ++failures; std::fprintf(stderr, "FAIL: %s\n", message); }
}
void pump() {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}
DWORD WINAPI finalize_on_worker(void *pointer) {
    delete static_cast<pet3d::WindowBridge *>(pointer);
    return 0;
}
HWND helper = nullptr;
BOOL CALLBACK find_helper(HWND hwnd, LPARAM) {
    wchar_t name[128]{};
    GetClassNameW(hwnd, name, 128);
    if (wcscmp(name, pet3d::WindowBridge::helper_class) == 0) helper = hwnd;
    return TRUE;
}
struct MenuContext {
    pet3d::WindowBridge *bridge;
    HWND role;
    LONG_PTR original_proc;
    bool worker_release;
    bool explicit_shutdown;
    bool entered = false;
};
UINT nested_menu_dispatch(HWND menu_owner, void *pointer) {
    auto &context = *static_cast<MenuContext *>(pointer);
    context.entered = true;
    check(pet3d::testing::live_states() == 1, "menu callback begins with one retained native state");
    if (context.explicit_shutdown) context.bridge->shutdown();
    if (context.worker_release) {
        HANDLE worker = CreateThread(nullptr, 0, finalize_on_worker, context.bridge, 0, nullptr);
        if (!worker) std::exit(2);
        const DWORD joined = WaitForSingleObject(worker, 1500);
        check(joined == WAIT_OBJECT_0, "worker final release inside menu cannot wait on owner");
        if (joined != WAIT_OBJECT_0) { pump(); WaitForSingleObject(worker, 1500); }
        CloseHandle(worker);
    } else {
        delete context.bridge;
    }
    context.bridge = nullptr;
    // Deliver the posted shutdown *inside* the outer show_menu call. This is
    // the reentrant path a real TrackPopupMenuEx modal loop would dispatch.
    pump();
    check(pet3d::testing::live_states() == 1, "facade deletion cannot free state under outer show_menu");
    check(IsWindow(menu_owner), "nested shutdown defers helper destruction until outer callback exits");
    check(GetWindowLongPtrW(context.role, GWLP_WNDPROC) != context.original_proc,
            "nested shutdown defers subclass teardown until outer callback exits");
    SendMessageW(menu_owner, WM_COMMAND, 2, 0);
    return 5; // ignored after shutdown request; continuation must still be safe
}
}

int main() {
    WNDCLASSW cls{};
    cls.hInstance = GetModuleHandleW(nullptr);
    cls.lpfnWndProc = DefWindowProcW;
    cls.lpszClassName = L"Pet3D.LifetimeTest";
    if (!RegisterClassW(&cls)) return 2;
    const HWND role = CreateWindowExW(WS_EX_TOOLWINDOW, cls.lpszClassName,
            L"hidden lifetime regression", WS_POPUP, -200, -200, 100, 100,
            nullptr, nullptr, cls.hInstance, nullptr);
    if (!role) return 2;
    const auto original_proc = GetWindowLongPtrW(role, GWLP_WNDPROC);
    const auto original_ex = GetWindowLongPtrW(role, GWL_EXSTYLE);
    check(pet3d::testing::live_states() == 0, "initial native state count");

    auto *bridge = new pet3d::WindowBridge;
    if (!bridge->configure(reinterpret_cast<int64_t>(role))) return 2;
    bridge->set_interactive(false);
    bridge->begin_capture();
    EnumThreadWindows(GetCurrentThreadId(), find_helper, 0);
    const HWND old_helper = helper;
    const HANDLE worker = CreateThread(nullptr, 0, finalize_on_worker, bridge, 0, nullptr);
    if (!worker) return 2;
    // Deliberately join WITHOUT dispatching messages: old SendMessage cleanup
    // waits on this thread and therefore cannot finish until this wait expires.
    const DWORD joined = WaitForSingleObject(worker, 1500);
    check(joined == WAIT_OBJECT_0, "worker finalization must finish while owner is joining without pumping");
    pump();
    check(WaitForSingleObject(worker, 1500) == WAIT_OBJECT_0, "worker eventually completes");
    CloseHandle(worker);
    check(!IsWindow(old_helper), "owner pump destroys orphan helper");
    check(GetCapture() != role, "deferred cleanup releases capture");
    check(GetWindowLongPtrW(role, GWLP_WNDPROC) == original_proc, "deferred cleanup restores WNDPROC");
    check(GetWindowLongPtrW(role, GWL_EXSTYLE) == original_ex, "deferred cleanup restores styles");
    check(pet3d::testing::live_states() == 0, "worker orphan native state reclaimed on owner thread");

    // Both owner/worker deletion, with and without an earlier reentrant explicit
    // shutdown, must keep the outer menu's callback state alive through return.
    for (bool worker_release : {false, true}) {
        for (bool explicit_shutdown : {false, true}) {
            auto *menu_bridge = new pet3d::WindowBridge;
            check(menu_bridge->configure(reinterpret_cast<int64_t>(role)), "configure nested menu fixture");
            MenuContext context{menu_bridge, role, original_proc, worker_release, explicit_shutdown};
            helper = nullptr;
            EnumThreadWindows(GetCurrentThreadId(), find_helper, 0);
            const HWND menu_owner = helper;
            pet3d::testing::set_menu_runner(nested_menu_dispatch, &context);
            // Handles both modern and legacy shell callback encoding; only one
            // event will invoke the menu runner for the negotiated icon version.
            SendMessageW(menu_owner, WM_APP + 0x3D, 0, WM_CONTEXTMENU);
            if (!context.entered) SendMessageW(menu_owner, WM_APP + 0x3D, 0, WM_RBUTTONUP);
            pet3d::testing::set_menu_runner(nullptr, nullptr);
            check(context.entered, "test enters real show_menu continuation through helper WNDPROC");
            check(!IsWindow(menu_owner), "outer menu return completes helper destruction");
            check(pet3d::testing::live_states() == 0, "native state reclaimed after outer menu return");
            check(GetWindowLongPtrW(role, GWLP_WNDPROC) == original_proc, "nested menu cleanup restores WNDPROC");
            check(GetWindowLongPtrW(role, GWL_EXSTYLE) == original_ex, "nested menu cleanup restores styles");
        }
    }

    // Check actual RegisterHotKey ownership without injecting keyboard input.
    constexpr int probe_id = 0x3D70;
    constexpr UINT modifiers = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT;
    const bool available = RegisterHotKey(nullptr, probe_id, modifiers, 'Q') != FALSE;
    check(available, "Ctrl+Alt+Q available before fixture configures");
    if (available) UnregisterHotKey(nullptr, probe_id);
    {
        pet3d::WindowBridge next;
        check(next.configure(reinterpret_cast<int64_t>(role)), "configure hotkey fixture");
        const bool stolen = RegisterHotKey(nullptr, probe_id, modifiers, 'Q') != FALSE;
        check(!stolen && GetLastError() == ERROR_HOTKEY_ALREADY_REGISTERED,
                "bridge reserves Ctrl+Alt+Q with RegisterHotKey");
        if (stolen) UnregisterHotKey(nullptr, probe_id);
        next.shutdown();
    }
    const bool released = RegisterHotKey(nullptr, probe_id, modifiers, 'Q') != FALSE;
    check(released, "shutdown releases Ctrl+Alt+Q registration");
    if (released) UnregisterHotKey(nullptr, probe_id);
    check(pet3d::testing::live_states() == 0, "no native state leaks after all fixtures");
    DestroyWindow(role);
    UnregisterClassW(cls.lpszClassName, cls.hInstance);
    std::printf("%s: %d lifetime/hotkey assertions, %d failures\n",
            failures ? "FAIL" : "PASS", assertions, failures);
    return failures ? 1 : 0;
}
