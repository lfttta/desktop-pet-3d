#include "window_state.hpp"
#include <cstdio>
#include <cassert>
#include <utility>
#ifdef PET_WINDOW_TESTING
#include "window_test_hooks.hpp"
#endif

namespace pet3d {
namespace {
void warn(const char *message) {
    std::fprintf(stderr, "PetWindowBridge: %s (Win32=%lu)\n", message, GetLastError());
}
bool set_ex_style(HWND hwnd, LONG_PTR style) {
    SetLastError(ERROR_SUCCESS);
    return SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style) != 0 || GetLastError() == ERROR_SUCCESS;
}
}



WindowBridge::WindowBridge() : state_(std::make_shared<WindowState>()) {}
WindowBridge::~WindowBridge() {
    auto state = std::move(state_);
    if (state) state->shutdown();
}
bool WindowBridge::configure(int64_t hwnd) { auto state = state_; return state->configure(hwnd); }
void WindowBridge::set_interactive(bool value) { auto state = state_; state->set_interactive(value); }
DesktopPoint WindowBridge::get_cursor_position() const { auto state = state_; return state->get_cursor_position(); }
double WindowBridge::get_dpi_scale() const { auto state = state_; return state->get_dpi_scale(); }
bool WindowBridge::is_left_button_down() const { auto state = state_; return state->is_left_button_down(); }
bool WindowBridge::begin_capture() { auto state = state_; return state->begin_capture(); }
void WindowBridge::end_capture() { auto state = state_; state->end_capture(); }
bool WindowBridge::has_capture() { auto state = state_; return state->has_capture(); }
int64_t WindowBridge::poll_tray_action() { auto state = state_; return state->poll_tray_action(); }
bool WindowBridge::set_menu_actions(const std::string &groups) { auto state = state_; return state->set_menu_actions(groups); }
void WindowBridge::shutdown() { auto state = state_; state->shutdown(); }
int64_t WindowBridge::acquire_instance(const std::string &key) { auto state = state_; return state->acquire_instance(key); }
bool WindowBridge::adopt_service_process(int64_t pid) { auto state = state_; return state->adopt_service_process(pid); }
void WindowBridge::terminate_service_processes() { auto state = state_; state->terminate_service_processes(); }
std::string WindowBridge::get_displays_json() const { auto state = state_; return state->get_displays_json(); }

#ifdef PET_WINDOW_TESTING
namespace testing {
std::atomic<int> state_count{0};
MenuRunner menu_runner = nullptr;
void *menu_context = nullptr;
int live_states() { return state_count.load(); }
void set_menu_runner(MenuRunner runner, void *context) {
    menu_runner = runner;
    menu_context = context;
}
}
#endif

WindowState::WindowState() {
#ifdef PET_WINDOW_TESTING
    ++testing::state_count;
#endif
}
WindowState::~WindowState() {
    assert(!role_ && !helper_ && callback_depth_ == 0);
    close_resources();
#ifdef PET_WINDOW_TESTING
    --testing::state_count;
#endif
}

WindowState::CallbackScope::CallbackScope(WindowState *self) : state(self->shared_from_this()) {
    ++state->callback_depth_;
    if (state->shutdown_requested_.load() && state->menu_open_) EndMenu();
}
WindowState::CallbackScope::~CallbackScope() {
    if (--state->callback_depth_ == 0 && state->shutdown_requested_.load()) state->finish_shutdown();
}

bool WindowState::on_thread() const {
    const DWORD thread = thread_.load();
    return !thread || thread == GetCurrentThreadId();
}

bool WindowState::configure(int64_t handle) {
    if (shutdown_requested_.load()) return false;
    const auto hwnd = reinterpret_cast<HWND>(static_cast<intptr_t>(handle));
    if (role_) return on_thread() && role_ == hwnd && IsWindow(role_);
    if (helper_ || !hwnd || !IsWindow(hwnd)) return false;
    DWORD process = 0;
    const DWORD thread = GetWindowThreadProcessId(hwnd, &process);
    if (process != GetCurrentProcessId() || thread != GetCurrentThreadId() ||
            (GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CHILD) ||
            GetPropW(hwnd, owner_property)) return false;

    thread_ = thread;
    role_ = hwnd;
    original_ex_ = GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
    interactive_ = true;
    stopping_ = false;
    configured_lifetime_ = shared_from_this();
    if (!SetPropW(hwnd, owner_property, this)) {
        role_ = nullptr;
        thread_ = 0;
        configured_lifetime_.reset();
        return false;
    }
    if (!SetWindowSubclass(hwnd, role_proc, reinterpret_cast<UINT_PTR>(this),
            reinterpret_cast<DWORD_PTR>(this))) {
        RemovePropW(hwnd, owner_property);
        role_ = nullptr;
        thread_ = 0;
        configured_lifetime_.reset();
        return false;
    }
    if (!apply_interaction() || !create_helper()) {
        warn("configure failed; restoring original window");
        shutdown();
        return false;
    }
    return true;
}

bool WindowState::apply_interaction() {
    if (!role_ || !IsWindow(role_)) return false;
    const LONG_PTR current = GetWindowLongPtrW(role_, GWL_EXSTYLE);
    LONG_PTR next = current | WS_EX_NOACTIVATE;
    if (interactive_ || capture_) {
        next &= ~WS_EX_TRANSPARENT;
        next = (next & ~WS_EX_LAYERED) | (original_ex_ & WS_EX_LAYERED);
    } else {
        // HTTRANSPARENT alone only skips windows on our thread. Layered +
        // transparent is the Win32 cross-process input passthrough mechanism.
        next |= WS_EX_LAYERED | WS_EX_TRANSPARENT;
    }
    // Do not call SetLayeredWindowAttributes/UpdateLayeredWindow: Godot owns
    // per-pixel D3D12 composition. No alpha/color key, show state, geometry,
    // frame reset or z-order operation is performed on the role HWND.
    if (next != current && !set_ex_style(role_, next)) {
        warn("could not apply interaction styles");
        return false;
    }
    return true;
}

void WindowState::set_interactive(bool interactive) {
    if (!on_thread() || shutdown_requested_.load()) return;
    interactive_ = interactive;
    if (role_) apply_interaction();
}

DesktopPoint WindowState::get_cursor_position() const {
    POINT point{};
    if (!GetPhysicalCursorPos(&point)) return {};
    return {point.x, point.y};
}

double WindowState::get_dpi_scale() const {
    // Effective per-monitor Windows DPI, not the panel's physical density.
    // Query each time so WM_DPICHANGED/moving between monitors needs no cache.
    const UINT dpi = role_ && IsWindow(role_) ? GetDpiForWindow(role_) : 0;
    return dpi ? static_cast<double>(dpi) / 96.0 : 1.0;
}

bool WindowState::is_left_button_down() const {
    return (GetAsyncKeyState(VK_LBUTTON) & 0x8000) != 0;
}

bool WindowState::begin_capture() {
    if (!on_thread() || !role_ || !IsWindow(role_) || stopping_ || shutdown_requested_.load()) return false;
    if (has_capture()) return true;
    // Do not steal capture from another window (including a native popup).
    if (GetCapture() && GetCapture() != role_) return false;
    capture_ = true;
    if (!apply_interaction()) {
        capture_ = false;
        return false;
    }
    SetCapture(role_);
    capture_ = GetCapture() == role_;
    if (!capture_) apply_interaction();
    return capture_;
}

void WindowState::end_capture() {
    if (!on_thread()) return;
    capture_ = false;
    if (role_ && GetCapture() == role_) ReleaseCapture();
    if (role_ && !stopping_) apply_interaction();
}

bool WindowState::has_capture() {
    if (!on_thread() || !role_) return false;
    if (capture_ && GetCapture() != role_) {
        capture_ = false;
        apply_interaction();
    }
    return capture_ && IsWindow(role_) && GetCapture() == role_;
}

void WindowState::queue_action(int64_t action) {
    if (action >= action_command_base) {
        // Reject indexes past the group table the owner actually sent.
        if (static_cast<size_t>(action - action_command_base) >= menu_action_count_) return;
    } else if (action < 1 || action > 7) {
        return;
    }
    if (stopping_ || shutdown_requested_.load()) return;
    if (action == 5) {
        exit_pending_ = true;
        if (menu_open_) EndMenu(); // allow the main loop to poll Exit immediately
        return;
    }
    if (count_ == actions_.size()) {
        read_ = (read_ + 1) % actions_.size();
        --count_;
    }
    actions_[(read_ + count_) % actions_.size()] = action;
    ++count_;
}

int64_t WindowState::poll_tray_action() {
    if (!on_thread() || shutdown_requested_.load()) return 0;
    if (exit_pending_) {
        exit_pending_ = false;
        read_ = count_ = 0;
        consume_activation(); // Exit also drains obsolete launch requests.
        return 5;
    }
    if (consume_activation()) return 2;
    if (!count_) return 0;
    const int64_t action = actions_[read_];
    read_ = (read_ + 1) % actions_.size();
    --count_;
    return action;
}

bool WindowState::set_menu_actions(const std::string &groups) {
    if (!on_thread()) return false;
    // Input: one group per line, "label|item|item". Items are action display
    // names in the order the owner wants them; the echoed command index maps
    // back to the flat position, so the owner keeps the authoritative list.
    std::vector<MenuGroup> parsed;
    size_t total = 0;
    std::string line;
    auto widen = [](const std::string &text) {
        if (text.empty()) return std::wstring();
        const int size = MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
        if (size <= 0) return std::wstring();
        std::wstring wide(static_cast<size_t>(size), L'\0');
        MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), wide.data(), size);
        return wide;
    };
    auto flush = [&]() {
        if (line.empty()) return;
        std::vector<std::wstring> items;
        size_t start = 0;
        std::wstring label;
        bool first = true;
        while (start <= line.size()) {
            const size_t bar = line.find('|', start);
            const std::string field = line.substr(start, bar == std::string::npos ? std::string::npos : bar - start);
            if (first) label = widen(field);
            else if (!field.empty()) items.push_back(widen(field));
            first = false;
            if (bar == std::string::npos) break;
            start = bar + 1;
        }
        if (!label.empty() && !items.empty()) {
            total += items.size();
            parsed.push_back({label, items});
        }
        line.clear();
    };
    for (const char character : groups) {
        if (character == '\n') flush();
        else if (character != '\r') line.push_back(character);
    }
    flush();
    menu_groups_ = std::move(parsed);
    menu_action_count_ = total;
    return !menu_groups_.empty();
}

NOTIFYICONDATAW WindowState::icon_data() const {
    NOTIFYICONDATAW data{};
    data.cbSize = sizeof(data);
    data.hWnd = helper_;
    data.uID = 1;
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP;
    data.uCallbackMessage = tray_message;
    data.hIcon = icon_;
    wcscpy_s(data.szTip, L"Pet3D 桌宠 | Ctrl+Alt+F10 恢复 / Ctrl+Alt+Q 退出");
    return data;
}

bool WindowState::add_tray_icon() {
    if (!helper_ || !icon_) return false;
    auto data = icon_data();
    // MODIFY also makes duplicate/replayed TaskbarCreated messages harmless.
    tray_added_ = Shell_NotifyIconW(NIM_MODIFY, &data) != FALSE;
    if (!tray_added_) tray_added_ = Shell_NotifyIconW(NIM_ADD, &data) != FALSE;
    if (tray_added_) {
        data.uVersion = NOTIFYICON_VERSION_4;
        tray_v4_ = Shell_NotifyIconW(NIM_SETVERSION, &data) != FALSE;
    }
    return tray_added_;
}

bool WindowState::create_helper() {
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
            GET_MODULE_HANDLE_EX_FLAG_PIN,
            reinterpret_cast<LPCWSTR>(&helper_proc), &module_)) return false;
    WNDCLASSEXW cls{};
    cls.cbSize = sizeof(cls);
    cls.hInstance = module_;
    cls.lpfnWndProc = helper_proc;
    cls.lpszClassName = helper_class;
    if (!RegisterClassExW(&cls)) {
        WNDCLASSEXW existing{};
        existing.cbSize = sizeof(existing);
        if (!GetClassInfoExW(module_, helper_class, &existing) || existing.lpfnWndProc != helper_proc)
            return false;
    }
    // A top-level helper, rather than HWND_MESSAGE, receives TaskbarCreated
    // broadcasts. It has no owner: focusing a tray menu cannot activate Godot.
    helper_ = CreateWindowExW(WS_EX_TOOLWINDOW, helper_class, L"Pet3D tray commands",
            WS_POPUP, -32000, -32000, 1, 1, nullptr, nullptr, module_, this);
    if (!helper_) return false;
    // Non-reloadable addon: pin code for the process lifetime so deferred
    // callbacks remain valid even if Godot releases its library handle.
    wake_window_.store(helper_);
    icon_ = CopyIcon(LoadIconW(nullptr, IDI_APPLICATION));
    taskbar_created_ = RegisterWindowMessageW(L"TaskbarCreated");
    recover_registered_ = RegisterHotKey(helper_, recover_hotkey,
            MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F10) != FALSE;
    exit_registered_ = RegisterHotKey(helper_, exit_hotkey,
            MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 'Q') != FALSE;
    const bool added = add_tray_icon();
    if (!SetTimer(helper_, retry_timer, 2000, nullptr)) return false;
    if (!added) warn("tray unavailable; Ctrl+Alt+Q exits, retrying every 2 seconds");
    if (!exit_registered_) warn("Ctrl+Alt+Q unavailable; use tray Exit");
    if (!recover_registered_) warn("Ctrl+Alt+F10 unavailable; use tray Recover position");
    // Never report successful configuration with neither tray nor exit fallback.
    return added || exit_registered_;
}

void WindowState::show_menu() {
    if (!helper_ || stopping_ || menu_open_ || shutdown_requested_.load()) return;
    end_capture();
    HMENU menu = CreatePopupMenu();
    if (!menu) { warn("unable to create tray menu; use Ctrl+Alt+Q"); return; }
    AppendMenuW(menu, MF_STRING, 1, L"显示 / 隐藏  |  Show / Hide (&S)");
    AppendMenuW(menu, MF_STRING, 2, L"恢复位置  |  Recover position (&R)");
    AppendMenuW(menu, MF_STRING, 4, L"待机 / 挥手  |  Idle / Wave (&W)");
    AppendMenuW(menu, MF_STRING, 6, L"设置  |  Settings (&T)");
    AppendMenuW(menu, MF_STRING, 7, L"聊天  |  Chat (&C)");
    if (!menu_groups_.empty()) {
        // One submenu per action category; commands echo action_command_base + flat index.
        HMENU actions = CreatePopupMenu();
        size_t flat = 0;
        if (actions) {
            for (const auto &group : menu_groups_) {
                HMENU submenu = CreatePopupMenu();
                if (!submenu) continue;
                for (const auto &item : group.items) {
                    InsertMenuW(submenu, static_cast<UINT>(-1), MF_BYPOSITION | MF_STRING,
                            static_cast<UINT_PTR>(action_command_base + flat), item.c_str());
                    ++flat;
                }
                AppendMenuW(actions, MF_POPUP, reinterpret_cast<UINT_PTR>(submenu), group.label.c_str());
            }
            AppendMenuW(menu, MF_POPUP, reinterpret_cast<UINT_PTR>(actions), L"动作  |  Actions (&A)");
        }
    }
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, 5, L"退出  |  Exit (&E)");
    SetMenuDefaultItem(menu, 1, FALSE);
    POINT point{};
    GetCursorPos(&point);
    // Native menus need a foreground owner for reliable dismissal/selection.
    // Show only the offscreen helper; never SetForegroundWindow(role_).
    menu_open_ = true;
    UINT command = 0;
#ifdef PET_WINDOW_TESTING
    if (testing::menu_runner) {
        // Deterministic nested dispatch without showing/activating any window.
        command = testing::menu_runner(helper_, testing::menu_context);
    } else
#endif
    {
        ShowWindow(helper_, SW_SHOWNOACTIVATE);
        SetForegroundWindow(helper_);
        if (!shutdown_requested_.load()) {
            command = TrackPopupMenuEx(menu,
                    TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                    point.x, point.y, helper_, nullptr);
        }
        PostMessageW(helper_, WM_NULL, 0, 0);
        ShowWindow(helper_, SW_HIDE);
    }
    menu_open_ = false;
    DestroyMenu(menu);
    queue_action(command);
}

void WindowState::shutdown() {
    shutdown_requested_.store(true);
    close_resources(); // Also works before configure, including worker release.
    // Beyond locked kernel resources the worker touches only atomics.
    // configured_lifetime_ keeps HWND userdata
    // alive after the facade is gone; callbacks each retain another reference.
    const DWORD owner = thread_.load();
    if (!owner) { shutdown_requested_.store(false); return; }
    if (owner != GetCurrentThreadId()) {
        const HWND wake = wake_window_.load();
        if (wake && !PostMessageW(wake, shutdown_message, reinterpret_cast<WPARAM>(this), 0))
            warn("cleanup wake could not be posted; owner callbacks/timer will retry");
        return;
    }
    if (menu_open_) EndMenu();
    if (!callback_depth_) finish_shutdown();
}

void WindowState::finish_shutdown() {
    assert(on_thread() && callback_depth_ == 0);
    if (stopping_) return;
    stopping_ = true;
    wake_window_.store(nullptr);
    end_capture();
    if (helper_) {
        KillTimer(helper_, retry_timer);
        if (recover_registered_) UnregisterHotKey(helper_, recover_hotkey);
        if (exit_registered_) UnregisterHotKey(helper_, exit_hotkey);
        // Delete unconditionally; Explorer can change state between callbacks.
        auto data = icon_data();
        Shell_NotifyIconW(NIM_DELETE, &data);
        DestroyWindow(helper_);
        helper_ = nullptr;
    }
    if (icon_) { DestroyIcon(icon_); icon_ = nullptr; }
    if (role_ && IsWindow(role_)) {
        // Preserve unrelated live app state (e.g. hidden/topmost) while restoring
        // exactly the bits we own. Never rewrite GWL_STYLE or position the role.
        const LONG_PTR ex = GetWindowLongPtrW(role_, GWL_EXSTYLE);
        if (!set_ex_style(role_, (ex & ~managed_styles) | (original_ex_ & managed_styles)))
            warn("could not restore original interaction styles");
        RemoveWindowSubclass(role_, role_proc, reinterpret_cast<UINT_PTR>(this));
        RemovePropW(role_, owner_property);
    }
    role_ = nullptr;
    if (module_) UnregisterClassW(helper_class, module_); // last helper owns removal
    module_ = nullptr;
    thread_ = 0;
    tray_added_ = tray_v4_ = recover_registered_ = exit_registered_ = false;
    taskbar_created_ = 0;
    read_ = count_ = 0;
    exit_pending_ = false;
    stopping_ = false;
    shutdown_requested_.store(false);
    configured_lifetime_.reset();
}

LRESULT CALLBACK WindowState::role_proc(HWND hwnd, UINT message, WPARAM wp, LPARAM lp,
        UINT_PTR id, DWORD_PTR data) {
    auto *self = reinterpret_cast<WindowState *>(data);
    CallbackScope callback(self);
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_POINTERACTIVATE) return PA_NOACTIVATE;
    if (message == WM_NCHITTEST && !self->interactive_ && !self->capture_) return HTTRANSPARENT;
    if (message == WM_RBUTTONUP && self->interactive_ && !self->capture_) {
        // Right-click on the character body: same menu as the tray icon, so the
        // 65-action list is reachable without hunting for the notification area.
        self->show_menu();
        return 0;
    }
    if (message == WM_CAPTURECHANGED) {
        self->capture_ = false;
        if (!self->stopping_) self->apply_interaction();
    } else if (message == WM_CANCELMODE) {
        self->end_capture();
    } else if (message == WM_NCDESTROY) {
        self->capture_ = false;
        self->role_ = nullptr;
        self->queue_action(5);
        RemovePropW(hwnd, owner_property);
        RemoveWindowSubclass(hwnd, role_proc, id);
    }
    // Common-controls maintains the original Godot WNDPROC/subclass chain.
    return DefSubclassProc(hwnd, message, wp, lp);
}

LRESULT CALLBACK WindowState::helper_proc(HWND hwnd, UINT message, WPARAM wp, LPARAM lp) {
    auto *self = reinterpret_cast<WindowState *>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    if (message == WM_NCCREATE) {
        self = static_cast<WindowState *>(reinterpret_cast<CREATESTRUCTW *>(lp)->lpCreateParams);
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
    }
    if (!self) return DefWindowProcW(hwnd, message, wp, lp);
    CallbackScope callback(self);
    if (self->taskbar_created_ && message == self->taskbar_created_ && !self->shutdown_requested_.load()) {
        self->tray_added_ = false;
        self->add_tray_icon();
        return 0;
    }
    switch (message) {
        case tray_message: {
            const UINT event = self->tray_v4_ ? LOWORD(lp) : static_cast<UINT>(lp);
            if ((self->tray_v4_ && (event == NIN_SELECT || event == NIN_KEYSELECT)) ||
                    (!self->tray_v4_ && event == WM_LBUTTONUP)) self->queue_action(1);
            if ((self->tray_v4_ && event == WM_CONTEXTMENU) ||
                    (!self->tray_v4_ && event == WM_RBUTTONUP)) self->show_menu();
            return 0;
        }
        case WM_TIMER:
            if (wp == retry_timer && !self->tray_added_ && !self->shutdown_requested_.load()) self->add_tray_icon();
            return 0;
        case WM_COMMAND:
            if (HIWORD(wp) == 0) self->queue_action(LOWORD(wp));
            return 0;
        case WM_HOTKEY:
            if (wp == recover_hotkey) self->queue_action(2);
            if (wp == exit_hotkey) self->queue_action(5);
            return 0;
        case WM_CLOSE:
            self->queue_action(5);
            return 0;
        case WM_QUERYENDSESSION:
            return TRUE;
        case WM_ENDSESSION:
            if (wp) self->shutdown();
            return 0;
        case shutdown_message:
            // Identity and pending flag make stale posts to a reused HWND inert.
            if (wp == reinterpret_cast<WPARAM>(self) && self->shutdown_requested_.load()) self->shutdown();
            return 0;
        case WM_NCDESTROY:
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            self->helper_ = nullptr;
            self->wake_window_.store(nullptr);
            if (!self->stopping_) self->shutdown();
            break;
    }
    return DefWindowProcW(hwnd, message, wp, lp);
}
} // namespace pet3d
