#pragma once
#ifndef PET_WINDOW_TESTING
#error "Test hooks must never be compiled into the delivered DLL"
#endif
#include "window_bridge.hpp"
namespace pet3d::testing {
// The runner receives the built root menu so tests can inspect command IDs.
using MenuRunner = UINT (*)(HWND helper, HMENU menu, void *context);
void set_menu_runner(MenuRunner runner, void *context);
int live_states();
enum class Failure { none, event, mutex, job_create, job_limit, job_assign,
    menu_actions_root, menu_submenu, display_interface };
void fail_next(Failure failure);
// One-shot injection consumer shared by both implementation TUs.
bool consume_failure(Failure failure);
std::string json_quote(const std::wstring &value);
}
