extends SceneTree

var failures: Array[String] = []

func check(value: bool, message: String) -> void:
	if not value:
		failures.append(message)
		printerr("FAIL: ", message)

func _initialize() -> void:
	check(ClassDB.class_exists("PetWindowBridge"), "PetWindowBridge must load")
	if not failures.is_empty():
		quit(1)
		return
	var expected := {
		"configure": [TYPE_BOOL, [TYPE_INT]],
		"set_interactive": [TYPE_NIL, [TYPE_BOOL]],
		"get_cursor_position": [TYPE_VECTOR2I, []],
		"get_dpi_scale": [TYPE_FLOAT, []],
		"is_left_button_down": [TYPE_BOOL, []],
		"begin_capture": [TYPE_BOOL, []],
		"end_capture": [TYPE_NIL, []],
		"has_capture": [TYPE_BOOL, []],
		"poll_tray_action": [TYPE_INT, []],
		"poll_wheel_delta": [TYPE_INT, []],
		"shutdown": [TYPE_NIL, []],
		"acquire_instance": [TYPE_INT, [TYPE_STRING]],
		"adopt_service_process": [TYPE_BOOL, [TYPE_INT]],
		"terminate_service_processes": [TYPE_NIL, []],
		"get_displays_json": [TYPE_STRING, []],
		"set_menu_actions": [TYPE_BOOL, [TYPE_STRING]],
		"configure_hotkeys": [TYPE_INT, [TYPE_STRING]],
		"get_status_json": [TYPE_STRING, []],
	}
	var methods := ClassDB.class_get_method_list("PetWindowBridge", true)
	check(methods.size() == expected.size(), "eighteen public native methods")
	for method in methods:
		var key: String = method.name
		check(expected.has(key), "unexpected method " + key)
		if not expected.has(key):
			continue
		check(method.return.type == expected[key][0], "return type " + key)
		var args: Array = method.args
		check(args.size() == expected[key][1].size(), "argument count " + key)
		for i in mini(args.size(), expected[key][1].size()):
			check(args[i].type == expected[key][1][i], "argument type " + key)
	if not failures.is_empty():
		quit(1)
		return
	for iteration in 50:
		var bridge = ClassDB.instantiate("PetWindowBridge")
		check(bridge is RefCounted, "RefCounted lifecycle")
		check(not bridge.configure(0), "zero HWND rejected")
		check(not bridge.configure(-1), "invalid HWND rejected")
		check(not bridge.begin_capture(), "capture requires configured HWND")
		check(not bridge.has_capture(), "no accidental capture")
		check(bridge.poll_tray_action() == 0, "unconfigured queue empty")
		check(bridge.poll_wheel_delta() == 0, "unconfigured wheel accumulator empty")
		check(typeof(bridge.get_cursor_position()) == TYPE_VECTOR2I, "cursor Variant conversion")
		check(typeof(bridge.get_dpi_scale()) == TYPE_FLOAT and bridge.get_dpi_scale() == 1.0,
			"unconfigured DPI float Variant conversion")
		check(typeof(bridge.is_left_button_down()) == TYPE_BOOL, "button Variant conversion")
		bridge.set_interactive(false)
		bridge.set_interactive(true)
		bridge.end_capture()
		bridge.shutdown()
		bridge.shutdown()
	var owner = ClassDB.instantiate("PetWindowBridge")
	var other = ClassDB.instantiate("PetWindowBridge")
	check(not owner.set_menu_actions(""), "empty menu payload rejected")
	check(owner.set_menu_actions("测试|甲|乙\n动作|丙"), "menu payload String Variant accepted")
	var status = JSON.parse_string(owner.get_status_json())
	check(status is Dictionary, "status JSON String Variant parses")
	if status is Dictionary:
		check(status.get("configured") == false and status.get("dpi_valid") == false,
			"status distinguishes unconfigured fallback values")
		check(status.get("dropped_actions") == 0, "status reports zero dropped actions")
		check(status.has("tray_added") and status.has("recover_hotkey") and status.has("exit_hotkey"),
			"status exposes tray/hotkey registration state")
	var instance_key := "native-api-中文-😀-\\\"-" + str(OS.get_process_id())
	check(owner.acquire_instance("") == -1, "empty instance key rejected")
	check(owner.acquire_instance(instance_key) == 1, "String Variant acquire before configure")
	check(other.acquire_instance(instance_key) == 0, "secondary String Variant signals owner")
	check(owner.poll_tray_action() == 2, "activation survives first startup before configure")
	check(owner.poll_tray_action() == 0, "activation consumed once")
	var fixture := ProjectSettings.globalize_path("res://../../../native/build/stage1_test.exe")
	var cross_owner = ClassDB.instantiate("PetWindowBridge")
	var cross_key := "native-cross-中文 😀-" + str(OS.get_process_id())
	check(cross_owner.acquire_instance(cross_key) == 1, "Unicode and spaces instance key")
	var launcher := OS.execute_with_pipe(fixture, ["--acquire", cross_key], true)
	check(launcher.has("pid"), "Godot creates real secondary process")
	if launcher.has("stdio"):
		check(launcher.stdio.get_line().strip_edges() == "0", "Godot UTF8 String key matches real subprocess")
		check(cross_owner.poll_tray_action() == 2, "real second launch recovery through Variant ABI")
		launcher.stdio.close()
		launcher.stderr.close()
	cross_owner.shutdown()
	check(not owner.adopt_service_process(OS.get_process_id()), "cannot adopt self")
	var display_text: String = owner.get_displays_json()
	var displays = JSON.parse_string(display_text)
	check(displays is Array and not displays.is_empty(), "String Variant display JSON")
	if displays is Array:
		for display in displays:
			check(display.id is String and not display.id.is_empty(), "stable display ID")
			check(display.name is String, "display name")
			check(display.position.size() == 2 and display.size.size() == 2, "display coordinates")
			check(display.work_area.size() == 4 and display.primary is bool, "display work area")
	print("DISPLAYS_JSON:", display_text)
	owner.shutdown()
	check(other.acquire_instance(instance_key) == 1, "preconfigure shutdown releases instance")
	other.shutdown()
	var service := OS.execute_with_pipe(fixture, ["--sleep"], false)
	check(service.has("pid"), "Godot pipe child spawned")
	if service.has("pid"):
		var service_pid: int = service.pid
		check(owner.adopt_service_process(service_pid), "adopt actual Godot OS.execute_with_pipe child via Variant ABI")
		owner.terminate_service_processes()
		for attempt in 100:
			if not OS.is_process_running(service_pid):
				break
			OS.delay_msec(10)
		check(not OS.is_process_running(service_pid), "job termination kills actual Godot pipe child via Variant ABI")
		service.stdio.close()
		service.stderr.close()
	owner.terminate_service_processes()
	owner.shutdown()
	# This script uses statically typed native calls, exercising ptrcall as well.
	var typed = load("res://typed_smoke.gd").new()
	typed.run()
	if failures.is_empty():
		print("PASS: eighteen-method API, original ten preserved, Variant and ptrcall, 50 RefCounted lifecycles")
	quit(0 if failures.is_empty() else 1)
