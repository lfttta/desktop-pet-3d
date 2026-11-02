#pragma once
#ifndef PET_WINDOW_TESTING
#error "Test hooks must never be compiled into the delivered DLL"
#endif
#include "window_bridge.hpp"
namespace pet3d::testing {
using MenuRunner = UINT (*)(HWND helper, void *context);
void set_menu_runner(MenuRunner runner, void *context);
int live_states();
enum class Failure { none, event, mutex, job_create, job_limit, job_assign };
void fail_next(Failure failure);
std::string json_quote(const std::wstring &value);
}
