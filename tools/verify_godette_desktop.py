"""Real HWND drag/passthrough and bounded two-minute CPU/memory observation."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import tempfile
import time

from verify_stage1_package import assert_clean_log, service_exchange, wait_pid_gone, wait_report

ROOT = Path(__file__).resolve().parents[1]
USER = c.WinDLL('user32', use_last_error=True)
KERNEL = c.WinDLL('kernel32', use_last_error=True)
PSAPI = c.WinDLL('psapi', use_last_error=True)
CALLBACK = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
USER.EnumWindows.argtypes = [CALLBACK, w.LPARAM]
USER.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
USER.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
USER.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
USER.GetWindowLongPtrW.argtypes = [w.HWND, c.c_int]
USER.GetWindowLongPtrW.restype = c.c_ssize_t
USER.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
USER.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
USER.WindowFromPoint.argtypes = [w.POINT]
USER.WindowFromPoint.restype = w.HWND
USER.GetForegroundWindow.restype = w.HWND
USER.GetSystemMetrics.argtypes = [c.c_int]
USER.GetSystemMetrics.restype = c.c_int
KERNEL.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
KERNEL.OpenProcess.restype = w.HANDLE
KERNEL.CloseHandle.argtypes = [w.HANDLE]
KERNEL.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4


class Memory(c.Structure):
    _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [(key, c.c_size_t) for key in
        ['peak_working', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private']]


PSAPI.GetProcessMemoryInfo.argtypes = [w.HANDLE, c.POINTER(Memory), w.DWORD]


def windows(pid: int) -> dict:
    result = {}
    @CALLBACK
    def visit(handle, _):
        owner = w.DWORD()
        USER.GetWindowThreadProcessId(handle, c.byref(owner))
        if owner.value == pid:
            title = c.create_unicode_buffer(256)
            USER.GetWindowTextW(handle, title, 256)
            result[title.value] = handle
        return True
    USER.EnumWindows(visit, 0)
    return result


def rectangle(handle):
    value = w.RECT()
    if not USER.GetWindowRect(handle, c.byref(value)):
        raise c.WinError(c.get_last_error())
    return value


def counters(pid: int) -> dict:
    handle = KERNEL.OpenProcess(0x1000 | 0x10, False, pid)
    if not handle:
        raise c.WinError(c.get_last_error())
    try:
        memory = Memory()
        memory.cb = c.sizeof(memory)
        if not PSAPI.GetProcessMemoryInfo(handle, c.byref(memory), memory.cb):
            raise c.WinError(c.get_last_error())
        times = [w.FILETIME() for _ in range(4)]
        if not KERNEL.GetProcessTimes(handle, *(c.byref(value) for value in times)):
            raise c.WinError(c.get_last_error())
        seconds = sum((item.dwHighDateTime << 32) | item.dwLowDateTime for item in times[2:]) / 1e7
        return {'cpu_seconds': seconds, 'working_bytes': memory.working, 'private_bytes': memory.private}
    finally:
        KERNEL.CloseHandle(handle)


def verify(package: Path, report: Path, duration: float = 120.0) -> None:
    report.mkdir(parents=True, exist_ok=True)
    # Coordinates remain physical pixels on displays using non-100% scaling.
    USER.SetProcessDpiAwarenessContext(c.c_void_p(-4))
    original_cursor = w.POINT()
    USER.GetCursorPos(c.byref(original_cursor))
    with tempfile.TemporaryDirectory(prefix='Godette-desktop-') as temporary:
        data = Path(temporary)
        service_exchange(package, data, [
            {'version': 1, 'id': 'save', 'type': 'settings.update', 'payload': {'scale': 1.0,
             'placement': {'x': 0.55, 'y': 0.75, 'edge_x': 0, 'edge_y': 0}}},
            {'version': 1, 'id': 'stop', 'type': 'shutdown', 'payload': {}},
        ])
        status = data / 'runtime.json'
        log = report / 'desktop.log'
        command = [str(package / 'Pet3D.exe'), '--log-file', str(log), '--', f'--data-dir={data}', f'--report={status}', f'--auto-exit={duration + 30}',
                   # The pet walks around on its own; this check injects a drag at a
                   # coordinate derived from a fixed standing pose, so an autonomous
                   # walk would move the hit region out from under the cursor.
                   '--no-behavior']
        app = subprocess.Popen(command, cwd=package, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        child = 0
        try:
            ready = wait_report(status, lambda value: value.get('service_state') == 'ready')
            child = int(ready['service_pid'])
            handles = windows(app.pid)
            role = handles['Pet3D']
            tray = handles['Pet3D tray commands']
            time.sleep(0.4)
            rect = rectangle(role)
            initial = (rect.left, rect.top)
            USER.SetCursorPos(rect.left + 15, rect.top + 15)
            time.sleep(0.2)
            blank_passthrough = bool(USER.GetWindowLongPtrW(role, -20) & 0x20)
            blank_target = USER.WindowFromPoint(w.POINT(rect.left + 15, rect.top + 15))
            assert blank_passthrough and blank_target != role, 'Transparent area did not pass through'
            width, height = rect.right - rect.left, rect.bottom - rect.top
            # Standing Hips are near y=.69 in the known orthographic camera.
            x = rect.left + width // 2
            y = rect.top + int(height * (0.5 + (0.9 - 0.69) / 3.0))
            USER.SetCursorPos(x, y)
            # A cold start of a freshly extracted package can spend longer than
            # one frame building the greeting bubble and its font, so poll
            # instead of trusting a single 250 ms sample. One run out of three
            # failed here while the same binary passed on the spot.
            body_interactive = False
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not bool(USER.GetWindowLongPtrW(role, -20) & 0x20):
                    body_interactive = True
                    break
                time.sleep(0.05)
            assert body_interactive, 'Body hit region did not become interactive'
            foreground = USER.GetForegroundWindow()
            USER.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.12)
            # Measure while the button is still down. Reading the rectangle after
            # mouse-up measured the fall, not the drag: the motion controller
            # deliberately drops a released pet to the bottom edge, so a reading
            # taken 0.25 s later is wherever gravity has carried it by then.
            dragged = None
            for step in range(1, 6):
                USER.SetCursorPos(x + step * 15, y - step * 8)
                time.sleep(0.06)
                dragged = rectangle(role)
            held_steps = [x + step * 15 for step in range(1, 6)]
            USER.mouse_event(0x0004, 0, 0, 0, 0)
            # Release behaviour is asserted separately below; let it settle first.
            time.sleep(1.2)
            landed = rectangle(role)
            released_delta = (landed.left - initial[0], landed.top - initial[1])
            # Print the geometry on every run: this assertion drives the real
            # cursor, so when it fails the numbers are the only evidence of
            # whether the role was clamped at a wall or simply did not follow.
            print('DESKTOP_DRAG start=%s probe=(%d,%d) size=(%d,%d) cursor_end=%d dragged=%s delta=(%d,%d) released_delta=(%d,%d)'
                  % (initial, x, y, width, height, held_steps[-1], (dragged.left, dragged.top),
                     dragged.left - initial[0], dragged.top - initial[1],
                     released_delta[0], released_delta[1]), flush=True)
            # Horizontal tracking must be close to 1:1. The vertical one is a
            # spring: the pet lags the cursor while it swings, so it only has to
            # move the right way and by a comparable amount.
            dx = dragged.left - initial[0]
            dy = dragged.top - initial[1]
            assert dx > 40, 'Drag did not move the role horizontally, delta=%d' % dx
            assert dy < -15, 'Drag did not lift the role, delta=%d' % dy
            assert dx < 130, 'Role moved further than the cursor, delta=%d' % dx
            assert USER.GetForegroundWindow() == foreground, 'Role stole focus during drag'
            # A released pet falls back to the work area's bottom edge. This is
            # the documented behaviour of the physics branch and must not be
            # passed off as a drag failure.
            screen_height = USER.GetSystemMetrics(1)   # SM_CYSCREEN, physical px
            assert landed.bottom >= screen_height - 260, 'Released role did not return to the desktop bottom edge, bottom=%d of %d' % (landed.bottom, screen_height)
            USER.SetCursorPos(original_cursor.x, original_cursor.y)
            USER.SendMessageW(tray, 0x0111, 1, 0)
            time.sleep(0.2)
            assert USER.GetWindowLongPtrW(role, -20) & 0x20, 'Hidden role intercepted input'
            USER.SendMessageW(tray, 0x0111, 1, 0)
            time.sleep(0.3)
            samples = []
            start = time.monotonic()
            next_action = 0.0
            next_sample = 0.0
            while time.monotonic() - start < duration:
                elapsed = time.monotonic() - start
                if elapsed >= next_sample:
                    samples.append({'elapsed': elapsed, 'app': counters(app.pid), 'service': counters(child)})
                    next_sample += 10
                    print(f'DESKTOP_SAMPLE {elapsed:.0f}s', flush=True)
                if elapsed >= 60 and elapsed >= next_action:
                    USER.SendMessageW(tray, 0x0111, 4, 0)
                    next_action = elapsed + 0.35
                time.sleep(0.1)
            samples.append({'elapsed': time.monotonic() - start, 'app': counters(app.pid), 'service': counters(child)})
            USER.SendMessageW(tray, 0x0111, 5, 0)
            if app.wait(timeout=12):
                raise AssertionError('Tray exit failed')
            wait_pid_gone(child)
            assert_clean_log(log)
            first, last = samples[1], samples[-1]
            wall = last['elapsed'] - first['elapsed']
            summary = {key: {'mean_cpu_one_core_percent': 100 * (last[key]['cpu_seconds'] - first[key]['cpu_seconds']) / wall,
                             'private_growth_bytes': last[key]['private_bytes'] - first[key]['private_bytes'],
                             'final_working_bytes': last[key]['working_bytes']} for key in ['app', 'service']}
            (report / 'desktop-performance.json').write_text(json.dumps({'duration_seconds': duration, 'idle_seconds': 60,
                'repeated_wave_seconds': max(0, duration - 60), 'transparent_passthrough': True, 'body_drag': True,
                'no_focus_steal': True, 'hide_restore': True, 'tray_exit': True, 'service_cleanup': True,
                'summary': summary, 'samples': samples}, indent=2), 'utf-8')
            print('GODETTE_DESKTOP_OK ' + json.dumps(summary), flush=True)
        finally:
            USER.mouse_event(0x0004, 0, 0, 0, 0)
            USER.SetCursorPos(original_cursor.x, original_cursor.y)
            if app.poll() is None:
                app.kill()
                app.wait(timeout=10)
            if child:
                wait_pid_gone(child)


if __name__ == '__main__':
    verify(ROOT / 'dist/stage-2-godette', ROOT / 'build/stage2/reports')
