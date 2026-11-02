#include "window_bridge.hpp"
#include "window_test_hooks.hpp"
#include <tlhelp32.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <thread>

namespace {
int assertions = 0;
void check(bool value, const char *message) {
    ++assertions;
    if (!value) { std::fprintf(stderr, "FAIL: %s (Win32=%lu)\n", message, GetLastError()); std::exit(1); }
}
std::wstring executable() {
    wchar_t path[32768]{};
    check(GetModuleFileNameW(nullptr, path, 32768) != 0, "test executable path");
    return path;
}
struct Child {
    PROCESS_INFORMATION process{};
    HANDLE output = nullptr;
    explicit Child(const std::wstring &arguments) {
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE write = nullptr;
        check(CreatePipe(&output, &write, &sa, 0) != FALSE, "child output pipe");
        check(SetHandleInformation(output, HANDLE_FLAG_INHERIT, 0) != FALSE, "parent pipe not inherited");
        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdOutput = startup.hStdError = write;
        startup.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
        std::wstring command = L"\"" + executable() + L"\" " + arguments;
        const bool created = CreateProcessW(nullptr, command.data(), nullptr, nullptr, TRUE,
                CREATE_NO_WINDOW, nullptr, nullptr, &startup, &process) != FALSE;
        CloseHandle(write);
        check(created, "hidden subprocess created with handle inheritance enabled");
    }
    ~Child() {
        if (WaitForSingleObject(process.hProcess, 0) == WAIT_TIMEOUT) {
            TerminateProcess(process.hProcess, 99);
            WaitForSingleObject(process.hProcess, 5000);
        }
        CloseHandle(process.hThread); CloseHandle(process.hProcess); CloseHandle(output);
    }
    int read_number() {
        std::string text;
        const auto deadline = GetTickCount64() + 5000;
        while (GetTickCount64() < deadline) {
            DWORD available = 0;
            if (!PeekNamedPipe(output, nullptr, 0, nullptr, &available, nullptr)) break;
            if (!available) { Sleep(1); continue; }
            char c = 0; DWORD read = 0;
            if (!ReadFile(output, &c, 1, &read, nullptr) || !read) break;
            if (c == '\n') return std::stoi(text);
            text += c;
        }
        check(false, "subprocess reported result before deadline");
        return -999;
    }
    bool alive() const { return WaitForSingleObject(process.hProcess, 0) == WAIT_TIMEOUT; }
    void exited() { check(WaitForSingleObject(process.hProcess, 5000) == WAIT_OBJECT_0, "subprocess exits in deadline"); }
};
std::string key() { return "stage1-native-" + std::to_string(GetCurrentProcessId()); }
std::wstring wide(const std::string &value) { return std::wstring(value.begin(), value.end()); }
std::string utf8(const std::wstring &value) {
    const int size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}
DWORD parent_pid() {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    PROCESSENTRY32W entry{}; entry.dwSize = sizeof(entry);
    DWORD result = 0;
    if (Process32FirstW(snapshot, &entry)) do {
        if (entry.th32ProcessID == GetCurrentProcessId()) { result = entry.th32ParentProcessID; break; }
    } while (Process32NextW(snapshot, &entry));
    CloseHandle(snapshot);
    return result;
}
void instance_tests() {
    pet3d::WindowBridge owner;
    check(owner.acquire_instance("") == -1, "empty key fails");
    check(owner.acquire_instance(std::string("x\0y", 3)) == -1, "embedded NUL fails");
    check(owner.acquire_instance(std::string("\xff", 1)) == -1, "invalid UTF8 fails");
    check(owner.acquire_instance(key()) == 1, "primary acquired before configure");
    check(owner.acquire_instance(key()) == 1, "same owner acquire is idempotent");
    check(owner.acquire_instance(key() + "other") == -1, "live owner cannot change key");
    { Child other(L"--acquire " + wide(key())); check(other.read_number() == 0, "real secondary notified primary"); other.exited(); }
    check(owner.poll_tray_action() == 2, "startup signal preserved without HWND");
    check(owner.poll_tray_action() == 0, "activation consumed once");
    check(!owner.configure(0), "invalid configure after acquisition");
    owner.shutdown(); owner.shutdown();
    { Child next(L"--acquire " + wide(key())); check(next.read_number() == 1, "shutdown releases owner token"); next.exited(); }
    { Child next(L"--acquire " + wide(key())); check(next.read_number() == 1, "process exit releases owner token"); next.exited(); }
    {
        auto *orphan = new pet3d::WindowBridge;
        check(orphan->acquire_instance(key()) == 1, "worker-release instance");
        std::thread worker([orphan] { delete orphan; }); worker.join();
        check(owner.acquire_instance(key()) == 1, "worker destruction before configure releases handles");
        owner.shutdown();
    }
    const std::wstring gate_name = L"Local\\Pet3D.NativeTest.Gate." + std::to_wstring(GetCurrentProcessId());
    HANDLE gate = CreateEventW(nullptr, TRUE, FALSE, gate_name.c_str());
    check(gate != nullptr, "contender exit gate");
    std::vector<std::unique_ptr<Child>> contenders;
    for (int n = 0; n < 12; ++n)
        contenders.push_back(std::make_unique<Child>(L"--contend " + wide(key()) + L" " + gate_name));
    int primaries = 0, secondaries = 0;
    Child *primary = nullptr;
    for (auto &child : contenders) {
        const int result = child->read_number();
        if (result == 1) { ++primaries; primary = child.get(); }
        else if (result == 0) ++secondaries;
    }
    check(primaries == 1 && secondaries == 11, "concurrent processes elect exactly one owner");
    SetEvent(gate);
    check(primary->read_number() == 2, "startup notifications retained until owner starts polling");
    for (auto &child : contenders) child->exited();
    CloseHandle(gate);
    check(owner.acquire_instance(key()) == 1, "concurrent owner exit releases token");
    owner.shutdown();
}
void job_tests() {
    pet3d::WindowBridge bridge;
    check(!bridge.adopt_service_process(0) && !bridge.adopt_service_process(-1) &&
            !bridge.adopt_service_process(0x100000000LL), "invalid PID range rejected");
    check(!bridge.adopt_service_process(GetCurrentProcessId()), "self rejected");
    const DWORD foreign = parent_pid();
    check(foreign != 0 && !bridge.adopt_service_process(foreign), "foreign parent PID rejected");
    HANDLE foreign_process = OpenProcess(SYNCHRONIZE, FALSE, foreign);
    check(foreign_process && WaitForSingleObject(foreign_process, 0) == WAIT_TIMEOUT, "foreign process remains alive");
    CloseHandle(foreign_process);
    {
        Child child(L"--sleep");
        check(bridge.adopt_service_process(child.process.dwProcessId), "adopt direct child");
        check(bridge.adopt_service_process(child.process.dwProcessId), "adopt same child idempotent");
        BOOL in_job = FALSE;
        check(IsProcessInJob(child.process.hProcess, nullptr, &in_job) && in_job, "child belongs to job");
        // This child starts AFTER job creation with inheritance enabled. If the
        // job handle leaks into either child, shutdown cannot kill the service.
        Child survivor(L"--sleep");
        bridge.shutdown(); child.exited();
        check(survivor.alive(), "unadopted own child survives job close");
    }
    {
        Child first(L"--sleep");
        check(bridge.adopt_service_process(first.process.dwProcessId), "first service adopted");
        Child rejected(L"--sleep");
        pet3d::testing::fail_next(pet3d::testing::Failure::job_assign);
        check(!bridge.adopt_service_process(rejected.process.dwProcessId), "assignment failure in existing job reported");
        check(first.alive() && rejected.alive(), "failed assignment preserves live service and unassigned child");
        TerminateProcess(first.process.hProcess, 3); first.exited();
        Child restarted(L"--sleep");
        check(bridge.adopt_service_process(restarted.process.dwProcessId), "same job adopts restarted service");
        bridge.shutdown(); restarted.exited();
    }
    {
        Child child(L"--sleep");
        auto *orphan = new pet3d::WindowBridge;
        check(orphan->adopt_service_process(child.process.dwProcessId), "worker-release service adopted");
        std::thread worker([orphan] { delete orphan; }); worker.join(); child.exited();
    }
    {
        Child host(L"--job-owner");
        const DWORD service_pid = static_cast<DWORD>(host.read_number());
        HANDLE service = OpenProcess(SYNCHRONIZE, FALSE, service_pid);
        check(service != nullptr, "external observer opens owned service");
        TerminateProcess(host.process.hProcess, 7); host.exited();
        check(WaitForSingleObject(service, 5000) == WAIT_OBJECT_0, "abrupt owner exit kills service");
        CloseHandle(service);
    }
}
void failure_tests() {
    DWORD before = 0, after = 0;
    GetProcessHandleCount(GetCurrentProcess(), &before);
    for (auto failure : {pet3d::testing::Failure::event, pet3d::testing::Failure::mutex}) {
        pet3d::WindowBridge bridge;
        pet3d::testing::fail_next(failure);
        check(bridge.acquire_instance(key()) == -1, "injected instance failure reported");
        check(bridge.acquire_instance(key()) == 1, "instance failure leaves no poisoned owner");
        bridge.shutdown();
    }
    for (auto failure : {pet3d::testing::Failure::job_create, pet3d::testing::Failure::job_limit,
            pet3d::testing::Failure::job_assign}) {
        pet3d::WindowBridge bridge;
        Child child(L"--sleep");
        pet3d::testing::fail_next(failure);
        check(!bridge.adopt_service_process(child.process.dwProcessId), "injected job failure reported");
        bridge.shutdown();
        check(child.alive(), "failed adoption never kills unassigned child");
        check(bridge.adopt_service_process(child.process.dwProcessId), "retry after adoption failure succeeds");
        bridge.shutdown(); child.exited();
    }
    GetProcessHandleCount(GetCurrentProcess(), &after);
    check(before == after, "failure paths leak no process handles");
}
void terminate_tests() {
    pet3d::WindowBridge bridge;
    bridge.terminate_service_processes(); // No job is a harmless no-op.
    check(bridge.acquire_instance(key()) == 1, "termination fixture owns instance");
    Child first(L"--sleep"), second(L"--sleep"), unadopted(L"--sleep");
    check(bridge.adopt_service_process(first.process.dwProcessId), "first termination child adopted");
    check(bridge.adopt_service_process(second.process.dwProcessId), "second termination child adopted");
    bridge.terminate_service_processes();
    first.exited(); second.exited();
    check(unadopted.alive(), "job termination cannot kill an unadopted process");
    bridge.terminate_service_processes();
    check(bridge.acquire_instance(key()) == 1, "service termination preserves instance ownership");
    pet3d::WindowBridge secondary;
    check(secondary.acquire_instance(key()) == 0 && bridge.poll_tray_action() == 2,
            "service termination preserves activation event");
    Child restart(L"--sleep");
    check(bridge.adopt_service_process(restart.process.dwProcessId), "same job accepts restart after explicit termination");
    bridge.terminate_service_processes(); restart.exited();
    Child final_service(L"--sleep");
    check(bridge.adopt_service_process(final_service.process.dwProcessId), "job remains reusable after repeated termination");
    bridge.shutdown(); final_service.exited();
    bridge.terminate_service_processes();
    check(unadopted.alive(), "shutdown after terminate preserves unrelated child");
}
void display_tests() {
    const auto previous = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_UNAWARE);
    check(previous != nullptr, "display caller temporarily DPI unaware");
    pet3d::WindowBridge bridge;
    const auto json = bridge.get_displays_json();
    check(AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(), DPI_AWARENESS_CONTEXT_UNAWARE),
            "display enumeration restores caller DPI context");
    SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    MONITORINFO info{}; info.cbSize = sizeof(info);
    POINT origin{};
    check(GetMonitorInfoW(MonitorFromPoint(origin, MONITOR_DEFAULTTOPRIMARY), &info) != FALSE, "physical primary monitor rect");
    const auto size = "\"size\":[" + std::to_string(info.rcMonitor.right - info.rcMonitor.left) + ',' +
            std::to_string(info.rcMonitor.bottom - info.rcMonitor.top) + ']';
    check(json.find(size) != std::string::npos, "JSON uses physical monitor size even for unaware caller");
    check(json.find("\"primary\":true") != std::string::npos, "JSON identifies primary");
    check(json == bridge.get_displays_json(), "display identity stable across repeated queries and DPI contexts");
    SetThreadDpiAwarenessContext(previous);
    std::wstring text = L"中文😀\"\\\n\t";
    text += L'\0'; text += L'\x01';
    check(pet3d::testing::json_quote(text) == u8"\"中文😀\\\"\\\\\\u000a\\u0009\\u0000\\u0001\"",
            "JSON escapes quote, slash, controls, embedded NUL and preserves Unicode UTF8");
    check(pet3d::testing::json_quote(L"") == "\"\"", "empty JSON String");
    std::printf("DISPLAYS_JSON:%s\n", json.c_str());
}
}
int wmain(int argc, wchar_t **argv) {
    if (argc >= 2 && wcscmp(argv[1], L"--sleep") == 0) { Sleep(20000); return 0; }
    if (argc >= 2 && wcscmp(argv[1], L"--job-owner") == 0) {
        pet3d::WindowBridge bridge;
        Child service(L"--sleep");
        check(bridge.adopt_service_process(service.process.dwProcessId), "child host adopts service");
        std::printf("%lu\n", service.process.dwProcessId); std::fflush(stdout);
        Sleep(20000); return 0;
    }
    if (argc >= 3) {
        pet3d::WindowBridge bridge;
        const std::wstring value(argv[2]);
        const int result = static_cast<int>(bridge.acquire_instance(utf8(value)));
        std::printf("%d\n", result); std::fflush(stdout);
        if (argc == 4 && result == 1) {
            HANDLE gate = OpenEventW(SYNCHRONIZE, FALSE, argv[3]);
            if (!gate || WaitForSingleObject(gate, 10000) != WAIT_OBJECT_0) return 2;
            CloseHandle(gate);
            std::printf("%lld\n", bridge.poll_tray_action()); std::fflush(stdout);
        }
        return result < 0 ? 2 : 0;
    }
    instance_tests(); job_tests(); failure_tests(); terminate_tests(); display_tests();
    check(pet3d::testing::live_states() == 0, "stage1 states reclaimed");
    std::printf("PASS: %d stage1 subprocess/resource assertions\n", assertions);
    return 0;
}
