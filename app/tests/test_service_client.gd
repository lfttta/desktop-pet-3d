extends SceneTree
const Client = preload("res://scripts/service_client.gd")
var checks := 0
var failures := 0
var connected_count := 0
func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)
func _initialize() -> void:
	call_deferred("_run")
func _wait_until(test: Callable, timeout := 5.0) -> bool:
	var deadline := Time.get_ticks_msec() + int(timeout * 1000)
	while not test.call() and Time.get_ticks_msec() < deadline:
		await process_frame
	return bool(test.call())
func _run() -> void:
	var client := Client.new()
	root.add_child(client)
	var data := "D:/danjigame/3D-pet-gtpdo/tests/stage1/client-data-%d" % OS.get_process_id()
	client.configure("D:/phthon lang/python.exe", PackedStringArray(["-u", "D:/danjigame/3D-pet-gtpdo/service/main.py", "--data-dir", data]))
	client.connected.connect(func(_settings, _warnings): connected_count += 1)
	check(client.start(), "real child starts through nonblocking Godot pipes")
	check(await _wait_until(func(): return client.state == "ready"), "real ready handshake arrives")
	if client.state != "ready":
		await client.stop(0.5)
		client.free()
		quit(1)
		return
	var id := client.update_settings({"user_name": "中文测试 🐾", "scale": 1.25})
	check(not id.is_empty(), "save request is accepted")
	check(await _wait_until(func(): return client.settings.get("user_name") == "中文测试 🐾"), "UTF8 settings return through real pipe")
	check(client.settings.scale == 1.25, "settings ack contains persisted scale")
	var old_pid := client.process_pid
	OS.kill(old_pid)
	check(await _wait_until(func(): return connected_count == 2 and client.state == "ready", 8), "one unexpected crash automatically restarts service")
	check(client.settings.user_name == "中文测试 🐾", "restart reads saved settings without replaying requests")
	OS.kill(client.process_pid)
	check(await _wait_until(func(): return client.state == "failed" and client.process_pid == 0), "second crash stops automatic retries")
	client.retry()
	check(await _wait_until(func(): return client.state == "ready"), "explicit retry can recover after retry budget")
	var final_pid := client.process_pid
	await client.stop()
	check(client.process_pid == 0 and not OS.is_process_running(final_pid), "shutdown acknowledgement leads to a fully exited child")
	client.free()
	print("SERVICE_CLIENT_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
