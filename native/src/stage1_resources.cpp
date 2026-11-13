#include "window_state.hpp"
#include <bcrypt.h>
#include <sddl.h>
#include <tlhelp32.h>
#include <limits>
#include <climits>
#include <cstdio>
#include <vector>
#include <utility>
#ifdef PET_WINDOW_TESTING
#include "window_test_hooks.hpp"
#endif

namespace pet3d {
namespace {
class Handle {
public:
    HANDLE value = nullptr;
    explicit Handle(HANDLE h = nullptr) : value(h) {}
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle &) = delete;
    Handle &operator=(const Handle &) = delete;
    HANDLE release() { return std::exchange(value, nullptr); }
    explicit operator bool() const { return value && value != INVALID_HANDLE_VALUE; }
};
#ifdef PET_WINDOW_TESTING
// Injection state lives in window_bridge.cpp so both TUs share one consumer.
#define FAIL_AT(point) testing::consume_failure(testing::Failure::point)
#else
#define FAIL_AT(point) false
#endif

std::wstring instance_name(const std::string &key) {
    if (key.empty() || key.find('\0') != std::string::npos || key.size() > INT_MAX ||
            !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, key.data(), static_cast<int>(key.size()), nullptr, 0))
        return {};
    Handle token;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token.value)) return {};
    DWORD bytes = 0;
    GetTokenInformation(token.value, TokenUser, nullptr, 0, &bytes);
    if (!bytes) return {};
    std::vector<unsigned char> buffer(bytes);
    if (!GetTokenInformation(token.value, TokenUser, buffer.data(), bytes, &bytes)) return {};
    LPWSTR sid = nullptr;
    if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER *>(buffer.data())->User.Sid, &sid)) return {};
    std::wstring name = L"Local\\Pet3D.Stage1.";
    name += sid;
    LocalFree(sid);
    name += L".";
    unsigned char digest[32]{};
    // SHA256 bounds the Win32 object name without truncating/colliding keys.
    if (BCryptHash(BCRYPT_SHA256_ALG_HANDLE, nullptr, 0,
            reinterpret_cast<PUCHAR>(const_cast<char *>(key.data())), static_cast<ULONG>(key.size()),
            digest, sizeof(digest)) < 0) return {};
    constexpr wchar_t hex[] = L"0123456789abcdef";
    for (const auto byte : digest) { name += hex[byte >> 4]; name += hex[byte & 15]; }
    return name;
}

std::string quote_json(const std::wstring &value) {
    if (value.empty()) return "\"\"";
    const int length = static_cast<int>(value.size());
    const int bytes = WideCharToMultiByte(CP_UTF8, 0, value.data(), length, nullptr, 0, nullptr, nullptr);
    std::string utf8(static_cast<size_t>(bytes), '\0');
    if (bytes) WideCharToMultiByte(CP_UTF8, 0, value.data(), length, utf8.data(), bytes, nullptr, nullptr);
    std::string result = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : utf8) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c < 32) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
        else result += static_cast<char>(c);
    }
    return result + '"';
}
struct Displays { std::string json = "["; bool failed = false; int order = 0; };
BOOL CALLBACK append_display(HMONITOR monitor, HDC, LPRECT, LPARAM context) {
    auto &out = *reinterpret_cast<Displays *>(context);
    const int index = out.order++;
    try {
        MONITORINFOEXW info{}; info.cbSize = sizeof(info);
        if (!GetMonitorInfoW(monitor, &info)) { out.failed = true; return FALSE; }
        std::wstring id, name = info.szDevice;
        if (!FAIL_AT(display_interface)) {
            for (DWORD i = 0;; ++i) {
                DISPLAY_DEVICEW device{}; device.cb = sizeof(device);
                if (!EnumDisplayDevicesW(info.szDevice, i, &device, EDD_GET_DEVICE_INTERFACE_NAME)) break;
                if (!(device.StateFlags & DISPLAY_DEVICE_ACTIVE)) continue;
                if (device.DeviceID[0]) id = device.DeviceID;
                if (device.DeviceString[0]) name = device.DeviceString;
                if (!id.empty()) break;
            }
        }
        // Remote/virtual drivers may omit monitor interfaces. Fall back to the
        // EnumDisplayMonitors position: for an unchanged display topology the
        // enumeration order repeats exactly, while \\.\DISPLAYn source names can
        // be renumbered by Windows between enumerations. This is a layout-scoped
        // fallback, never a persistent hardware ID; replugging monitors may
        // renumber it, so the owner should re-resolve by name/geometry then.
        if (id.empty()) id = L"index:" + std::to_wstring(index);
        const auto &r = info.rcMonitor;
        const auto &w = info.rcWork;
        if (out.json.size() > 1) out.json += ',';
        out.json += "{\"id\":" + quote_json(id) + ",\"name\":" + quote_json(name) +
                ",\"position\":[" + std::to_string(r.left) + ',' + std::to_string(r.top) +
                "],\"size\":[" + std::to_string(r.right - r.left) + ',' + std::to_string(r.bottom - r.top) +
                "],\"work_area\":[" + std::to_string(w.left) + ',' + std::to_string(w.top) + ',' +
                std::to_string(w.right - w.left) + ',' + std::to_string(w.bottom - w.top) +
                "],\"primary\":" + ((info.dwFlags & MONITORINFOF_PRIMARY) ? "true}" : "false}");
        return TRUE;
    } catch (...) { out.failed = true; return FALSE; }
}
}

#ifdef PET_WINDOW_TESTING
namespace testing {
std::string json_quote(const std::wstring &value) { return quote_json(value); }
}
#endif

int64_t WindowState::acquire_instance(const std::string &key) {
    if (!on_thread() || shutdown_requested_.load()) return -1;
    std::lock_guard<std::mutex> lock(resources_mutex_);
    if (shutdown_requested_.load()) return -1;
    if (instance_mutex_) return key == instance_key_ ? 1 : -1;
    const std::wstring name = instance_name(key);
    if (name.empty()) return -1;
    // Open the auto-reset event BEFORE publishing the mutex. Never ResetEvent:
    // a secondary can signal while the primary is still starting/configuring.
    Handle event(FAIL_AT(event) ? nullptr : CreateEventW(nullptr, FALSE, FALSE, (name + L".activate").c_str()));
    if (!event) return -1;
    Handle mutex(FAIL_AT(mutex) ? nullptr : CreateMutexW(nullptr, FALSE, (name + L".owner").c_str()));
    const DWORD created = GetLastError();
    if (!mutex) return -1;
    if (created == ERROR_ALREADY_EXISTS) {
        // Only the primary retains a mutex handle. It is a process-lifetime
        // presence token, deliberately not a thread-owned waitable lock; this
        // permits safe final release on a RefCounted worker thread.
        CloseHandle(mutex.release());
        return SetEvent(event.value) ? 0 : -1;
    }
    instance_key_ = key;
    activation_event_ = event.release();
    instance_mutex_ = mutex.release();
    return 1;
}

bool WindowState::adopt_service_process(int64_t pid) {
    if (!on_thread() || shutdown_requested_.load() || pid <= 0 || pid > MAXDWORD ||
            pid == GetCurrentProcessId()) return false;
    std::lock_guard<std::mutex> lock(resources_mutex_);
    if (shutdown_requested_.load()) return false;
    Handle process(OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION |
            SYNCHRONIZE, FALSE, static_cast<DWORD>(pid)));
    if (!process || WaitForSingleObject(process.value, 0) != WAIT_TIMEOUT) return false;
    // Open and retain the handle before taking the snapshot, preventing PID
    // recycling between identity validation and AssignProcessToJobObject.
    Handle snapshot(CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0));
    if (!snapshot) return false;
    PROCESSENTRY32W entry{}; entry.dwSize = sizeof(entry);
    bool own_child = false;
    if (Process32FirstW(snapshot.value, &entry)) do {
        if (entry.th32ProcessID == static_cast<DWORD>(pid)) {
            own_child = entry.th32ParentProcessID == GetCurrentProcessId();
            break;
        }
    } while (Process32NextW(snapshot.value, &entry));
    if (!own_child) return false;
    FILETIME child_created{}, parent_created{}, exit{}, kernel{}, user{};
    if (!GetProcessTimes(process.value, &child_created, &exit, &kernel, &user) ||
            !GetProcessTimes(GetCurrentProcess(), &parent_created, &exit, &kernel, &user) ||
            CompareFileTime(&child_created, &parent_created) < 0) return false;
    if (service_job_) {
        BOOL same_job = FALSE;
        if (!IsProcessInJob(process.value, service_job_, &same_job)) return false;
        return same_job || (!FAIL_AT(job_assign) && AssignProcessToJobObject(service_job_, process.value));
    }
    // NULL security attributes => non-inheritable, unnamed job. Never expose
    // its handle to OS.execute_with_pipe's child, including later restarts.
    Handle job(FAIL_AT(job_create) ? nullptr : CreateJobObjectW(nullptr, nullptr));
    if (!job || !SetHandleInformation(job.value, HANDLE_FLAG_INHERIT, 0)) return false;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (FAIL_AT(job_limit) || !SetInformationJobObject(job.value, JobObjectExtendedLimitInformation,
            &limits, sizeof(limits))) return false;
    if (FAIL_AT(job_assign) || !AssignProcessToJobObject(job.value, process.value)) return false;
    service_job_ = job.release();
    return true;
}

void WindowState::terminate_service_processes() {
    if (!on_thread() || shutdown_requested_.load()) return;
    std::lock_guard<std::mutex> lock(resources_mutex_);
    if (service_job_ && !TerminateJobObject(service_job_, 0))
        std::fprintf(stderr, "PetWindowBridge: could not terminate service job (Win32=%lu)\n", GetLastError());
    // Keep the job handle and KILL_ON_JOB_CLOSE policy for later adoptions.
    // No PID lookup/kill occurs: only members of our validated, unnamed job die.
}

void WindowState::close_resources() {
    std::lock_guard<std::mutex> lock(resources_mutex_);
    if (service_job_) { CloseHandle(service_job_); service_job_ = nullptr; }
    // Close the mutex LAST: a new primary cannot inherit an event that the old
    // owner's polling loop might still consume after releasing ownership.
    if (activation_event_) { CloseHandle(activation_event_); activation_event_ = nullptr; }
    if (instance_mutex_) { CloseHandle(instance_mutex_); instance_mutex_ = nullptr; }
    instance_key_.clear();
}
bool WindowState::consume_activation() {
    std::lock_guard<std::mutex> lock(resources_mutex_);
    return activation_event_ && WaitForSingleObject(activation_event_, 0) == WAIT_OBJECT_0;
}
std::string WindowState::get_displays_json() const {
    // Thread-scoped PMv2 also works before configure and restores Godot's
    // previous context. Coordinates must remain physical at mixed scaling.
    struct DpiScope {
        DPI_AWARENESS_CONTEXT previous = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        ~DpiScope() { if (previous) SetThreadDpiAwarenessContext(previous); }
    } dpi;
    if (!dpi.previous) return "[]";
    Displays result;
    if (!EnumDisplayMonitors(nullptr, nullptr, append_display, reinterpret_cast<LPARAM>(&result)) || result.failed)
        return "[]";
    return result.json + ']';
}
} // namespace pet3d
