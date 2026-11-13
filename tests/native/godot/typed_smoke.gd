extends RefCounted

func run() -> void:
	var bridge: PetWindowBridge = PetWindowBridge.new()
	assert(not bridge.configure(0))
	bridge.set_interactive(false)
	var cursor: Vector2i = bridge.get_cursor_position()
	var dpi_scale: float = bridge.get_dpi_scale()
	assert(dpi_scale == 1.0)
	var pressed: bool = bridge.is_left_button_down()
	assert(not bridge.begin_capture())
	bridge.end_capture()
	assert(not bridge.has_capture())
	assert(bridge.poll_tray_action() == 0)
	assert(bridge.poll_wheel_delta() == 0)
	var key: String = "typed-中文-😀-" + str(OS.get_process_id())
	var acquired: int = bridge.acquire_instance(key)
	assert(acquired == 1)
	var other: PetWindowBridge = PetWindowBridge.new()
	assert(other.acquire_instance(key) == 0)
	assert(bridge.poll_tray_action() == 2)
	var adopted: bool = bridge.adopt_service_process(OS.get_process_id())
	assert(not adopted)
	var text: String = "previous contents"
	for iteration in 100:
		text = bridge.get_displays_json()
		assert(JSON.parse_string(text) is Array)
	var menu_ok: bool = bridge.set_menu_actions("测试|甲|乙")
	assert(menu_ok)
	var status: String = bridge.get_status_json()
	var parsed: Dictionary = JSON.parse_string(status)
	assert(parsed is Dictionary and parsed.get("dropped_actions") == 0)
	bridge.shutdown()
	assert(other.acquire_instance(key) == 1)
	other.shutdown()
	var fixture: String = ProjectSettings.globalize_path("res://../../../native/build/stage1_test.exe")
	var service: Dictionary = OS.execute_with_pipe(fixture, ["--sleep"], false)
	assert(service.has("pid"))
	var service_pid: int = service.pid
	assert(bridge.adopt_service_process(service_pid))
	bridge.terminate_service_processes()
	for attempt in 100:
		if not OS.is_process_running(service_pid):
			break
		OS.delay_msec(10)
	assert(not OS.is_process_running(service_pid))
	service.stdio.close()
	service.stderr.close()
	var restarted: Dictionary = OS.execute_with_pipe(fixture, ["--sleep"], false)
	assert(restarted.has("pid") and bridge.adopt_service_process(restarted.pid))
	bridge.shutdown()
	for attempt in 100:
		if not OS.is_process_running(restarted.pid):
			break
		OS.delay_msec(10)
	assert(not OS.is_process_running(restarted.pid))
	restarted.stdio.close()
	restarted.stderr.close()
	print("PASS: typed API cursor=", cursor, " left=", pressed, " dpi_scale=", dpi_scale)
