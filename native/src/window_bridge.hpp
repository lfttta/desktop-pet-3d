#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <cstdint>
#include <memory>
#include <string>

namespace pet3d {
struct DesktopPoint { int32_t x = 0; int32_t y = 0; };
class WindowState;

// Godot-owned facade. Native callbacks retain WindowState, never this object.
// Ordinary methods require the owner thread. Worker shutdown/final release is
// nonblocking and requests cleanup once active owner callbacks have unwound.
class WindowBridge final {
public:
    static constexpr wchar_t helper_class[] = L"Pet3D.TrayBridge.v1";
    static constexpr int recover_hotkey = 0x3D10;
    static constexpr int exit_hotkey = 0x3D12;
    WindowBridge();
    ~WindowBridge();
    WindowBridge(const WindowBridge &) = delete;
    WindowBridge &operator=(const WindowBridge &) = delete;

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
    std::shared_ptr<WindowState> state_;
};
} // namespace pet3d
