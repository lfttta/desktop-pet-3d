#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <commctrl.h>
#include <shellapi.h>
#include <array>
#include <atomic>
#include <memory>
#include "window_bridge.hpp"
#include <cstdint>
#include <mutex>

namespace pet3d {


// HWNDs retain this state independently of the Godot facade. All fields except
// the explicit atomics and resources under resources_mutex_ belong to the
// owning thread. Kernel-only resources can be closed safely by worker shutdown.
class WindowState final : public std::enable_shared_from_this<WindowState> {
public:
    static constexpr auto helper_class = WindowBridge::helper_class;
    static constexpr int recover_hotkey = WindowBridge::recover_hotkey;
    static constexpr int exit_hotkey = WindowBridge::exit_hotkey;
    WindowState();
    ~WindowState();
    WindowState(const WindowState &) = delete;
    WindowState &operator=(const WindowState &) = delete;

    bool configure(int64_t hwnd);
    void set_interactive(bool interactive);
    DesktopPoint get_cursor_position() const;
    double get_dpi_scale() const;
    bool is_left_button_down() const;
    bool begin_capture();
    void end_capture();
    bool has_capture();
    int64_t poll_tray_action();
    void shutdown();
    int64_t acquire_instance(const std::string &key);
    bool adopt_service_process(int64_t pid);
    void terminate_service_processes();
    std::string get_displays_json() const;

private:
    static constexpr UINT tray_message = WM_APP + 0x3D;
    static constexpr UINT shutdown_message = WM_APP + 0x3E;
    static constexpr UINT_PTR retry_timer = 1;
    static constexpr LONG_PTR managed_styles = WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT;
    static constexpr wchar_t owner_property[] = L"Pet3D.WindowBridge.Owner.v1";
    HWND role_ = nullptr;
    HWND helper_ = nullptr;
    HINSTANCE module_ = nullptr;
    HICON icon_ = nullptr;
    std::atomic<DWORD> thread_{0};
    std::atomic<HWND> wake_window_{nullptr};
    std::atomic<bool> shutdown_requested_{false};
    std::shared_ptr<WindowState> configured_lifetime_;
    unsigned callback_depth_ = 0;
    LONG_PTR original_ex_ = 0;
    bool interactive_ = true;
    bool capture_ = false;
    bool tray_added_ = false;
    bool tray_v4_ = false;
    bool recover_registered_ = false;
    bool exit_registered_ = false;
    bool stopping_ = false;
    bool menu_open_ = false;
    bool exit_pending_ = false;
    UINT taskbar_created_ = 0;
    std::array<int64_t, 32> actions_{};
    size_t read_ = 0;
    size_t count_ = 0;
    std::mutex resources_mutex_;
    HANDLE instance_mutex_ = nullptr;
    HANDLE activation_event_ = nullptr;
    HANDLE service_job_ = nullptr;
    std::string instance_key_;

    void close_resources();
    bool consume_activation();

    bool on_thread() const;
    void finish_shutdown();
    // Retain state across the entire callback, including DefSubclassProc and
    // nested menu dispatch. Native teardown occurs only after the outer frame.
    struct CallbackScope {
        std::shared_ptr<WindowState> state;
        explicit CallbackScope(WindowState *self);
        ~CallbackScope();
    };
    bool apply_interaction();
    bool create_helper();
    bool add_tray_icon();
    NOTIFYICONDATAW icon_data() const;
    void queue_action(int64_t action);
    void show_menu();
    static LRESULT CALLBACK role_proc(HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR);
    static LRESULT CALLBACK helper_proc(HWND, UINT, WPARAM, LPARAM);
};
} // namespace pet3d
