extends SceneTree
## Desktop sensing controls on the autonomy page: backfill, save patch and the
## disabled state while the service is down.

const SettingsWindow = preload("res://scripts/ui/settings_window.gd")

var checks := 0
var failures := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	await _test_controls_and_save()
	await _test_unavailable()
	await _test_legacy_schema()
	print("DESKTOP_SETTINGS_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _settings() -> Dictionary:
	return {
		"schema_version": 5,
		"autonomy": {
			"enabled": true,
			"cloud_enabled": true,
			"quiet_start": "22:30",
			"quiet_end": "07:15",
		},
		"desktop": {
			"enabled": true,
			"poll_seconds": 45,
			"idle_after_seconds": 900,
			"fullscreen_policy": "minimal",
			"dnd_policy": "minimal",
			"work_focus_policy": "normal",
			"idle_policy": "gentle",
		},
	}

func _test_controls_and_save() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("ready", "后台服务已连接")
	window.populate(_settings(), [])
	await process_frame

	check(window.desktop_enabled != null, "autonomy page has a desktop_enabled switch")
	check(window.desktop_idle != null, "autonomy page has a desktop_idle spin box")
	check(window.desktop_idle.min_value == 30 and window.desktop_idle.max_value == 86400,
		"desktop idle seconds span the full service range 30 to 86400")
	check(window.fullscreen_policy != null, "autonomy page has a fullscreen_policy option")
	check(window.work_focus_policy != null, "autonomy page has a work_focus_policy option")
	check(window.idle_policy != null, "autonomy page has an idle_policy option")
	check(window.cloud_proactive != null, "autonomy page has a cloud_proactive switch")
	check(window.desktop_state_label != null, "autonomy page has a desktop state label")

	check(window.desktop_enabled.button_pressed, "populate restores desktop.enabled")
	check(window.desktop_idle.value == 900, "populate restores desktop.idle_after_seconds")
	check(window.cloud_proactive.button_pressed, "populate restores autonomy.cloud_enabled")
	check(window._policy_value(window.fullscreen_policy, "") == "minimal",
		"populate restores fullscreen_policy")
	check(window._policy_value(window.work_focus_policy, "") == "normal",
		"populate restores work_focus_policy")
	check(window._policy_value(window.idle_policy, "") == "gentle",
		"populate restores idle_policy")
	check(window.quiet_start_input.text == "22:30", "populate keeps quiet hours")

	var requested: Dictionary = fake.requests[fake.requests.size() - 1]
	check(requested.type == "desktop.status", "populate asks for desktop.status")
	check(is_equal_approx(float(requested.timeout), 10.0), "desktop.status uses a 10 second timeout")
	fake.reply(requested.id, "result", {
		"enabled": true,
		"state": "fullscreen",
		"policy": {"allowed": false, "reason": "fullscreen"},
	})
	check(window.desktop_state_label.text.contains("全屏"),
		"desktop status label names the fullscreen state")
	check(window.desktop_state_label.text.contains("主动消息已暂停"),
		"desktop status label reports paused proactive messages")
	window._refresh_controls()
	check(window.desktop_state_label.text.contains("全屏"),
		"a rendered desktop label survives a later control refresh")

	# A flat "allowed" key is accepted as well.
	window._render_desktop_status({"enabled": true, "state": "active_work", "allowed": true})
	check(window.desktop_state_label.text.contains("专注工作"),
		"a flat status document still renders its state")
	check(not window.desktop_state_label.text.contains("已暂停"),
		"an allowed flat status leaves the pause suffix off")
	window._render_desktop_status({"enabled": true, "state": "idle", "policy": {"allowed": false}})
	check(window.desktop_state_label.text.contains("空闲") and window.desktop_state_label.text.contains("已暂停"),
		"a paused idle state renders both the label and the pause")

	# A transient sampling failure is not a disabled switch.
	window._render_desktop_status({"enabled": true, "state": "unknown", "reason": "sampling_failed"})
	check(window.desktop_state_label.text.contains("暂时无法读取"),
		"a sampling failure renders as unavailable, not as switched off")
	check(not window.desktop_state_label.text.contains("已关闭"),
		"a sampling failure never looks like a user setting")

	# A second populate inside the throttle window must not poll again.
	var before := fake.requests.size()
	window.populate(_settings(), [])
	check(fake.requests.size() == before, "populate within 10 seconds skips the desktop poll")

	window._select_policy(window.fullscreen_policy, "silent")
	window._select_policy(window.work_focus_policy, "minimal")
	window._select_policy(window.idle_policy, "normal")
	window.desktop_idle.value = 1200
	window.cloud_proactive.button_pressed = false

	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "save emits one settings patch")
	var patch: Dictionary = patches[0]
	check(patch.has("desktop"), "save patch includes the desktop group")
	check(patch.desktop.enabled == true, "save patch carries desktop.enabled")
	check(int(patch.desktop.poll_seconds) == 45,
		"save patch round-trips the service's poll_seconds instead of pinning 3")
	check(int(patch.desktop.idle_after_seconds) == 1200, "save patch carries the idle threshold")
	check(str(patch.desktop.fullscreen_policy) == "silent", "save patch carries fullscreen_policy")
	check(str(patch.desktop.dnd_policy) == "minimal",
		"save patch round-trips the service's dnd_policy instead of pinning silent")
	check(str(patch.desktop.work_focus_policy) == "minimal", "save patch carries work_focus_policy")
	check(str(patch.desktop.idle_policy) == "normal", "save patch carries idle_policy")
	check(patch.autonomy.has("cloud_enabled"), "save patch includes autonomy.cloud_enabled")
	check(patch.autonomy.cloud_enabled == false, "save patch carries the cloud switch")
	check(patch.autonomy.has("quiet_start") and patch.autonomy.quiet_start == "22:30",
		"save patch keeps the existing quiet hours")

	window.free()
	host.free()

func _test_unavailable() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	fake.state = "stopped"
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("stopped", "后台服务未连接")
	window.populate({}, [])
	await process_frame

	check(window.desktop_enabled.disabled, "desktop sensing is disabled while the service is down")
	check(window.desktop_idle.editable == false, "idle seconds are locked while the service is down")
	check(window.fullscreen_policy.disabled, "fullscreen policy is disabled while the service is down")
	check(window.work_focus_policy.disabled, "work focus policy is disabled while the service is down")
	check(window.idle_policy.disabled, "idle policy is disabled while the service is down")
	check(window.cloud_proactive.disabled, "cloud proactive is disabled while the service is down")

	# A service that predates schema v5 rejects the desktop group, so the
	# controls stay locked instead of letting the user edit an unsaveable value.
	window.show_status("ready", "后台服务已连接")
	window.populate({}, [])
	await process_frame
	check(window.desktop_enabled.disabled, "desktop controls stay locked for a pre-v5 service")

	# Reconnect with a v5 service, then let it report the command as missing.
	window.show_status("ready", "后台服务已连接")
	window._desktop_status_due = 0  # emulate the 10 second throttle expiring
	window.populate(_settings(), [])
	await process_frame
	check(not window.desktop_enabled.disabled, "controls unlock once a v5 service is ready")
	var request: Dictionary = fake.requests[fake.requests.size() - 1]
	check(request.type == "desktop.status", "the ready populate issues desktop.status")
	fake.reply(request.id, "error", {"code": "not_available", "message": "未接入"})
	check(window.desktop_state_label.text == "桌面感知未接入。",
		"not_available renders the desktop placeholder")
	check(window.desktop_enabled.disabled, "not_available disables the desktop switch")
	check(window.desktop_idle.editable == false, "not_available locks the idle threshold")
	check(window.fullscreen_policy.disabled, "not_available disables the fullscreen policy")
	check(window.work_focus_policy.disabled, "not_available disables the work focus policy")
	check(window.idle_policy.disabled, "not_available disables the idle policy")

	# A reconnect must invalidate the old reading and bypass the throttle so the
	# label does not stay on the disconnected text.
	window.show_status("failed", "后台服务意外退出")
	window.show_status("ready", "后台服务已连接")
	var before := fake.requests.size()
	window.open()
	check(fake.requests.size() == before + 1 and fake.requests[-1].type == "desktop.status",
		"a reconnect re-queries the desktop state on open")

	# A failed query must not leave the label on the querying placeholder.
	var pending: Dictionary = fake.requests[-1]
	fake.reply(pending.id, "error", {"code": "timeout", "message": "响应超时"})
	check(not window.desktop_state_label.text.contains("查询中"),
		"a timed out desktop.status leaves the querying placeholder")

	window.free()
	host.free()

func _test_legacy_schema() -> void:
	# A v4 service rejects the desktop group and autonomy.cloud_enabled, so the
	# save must omit both instead of failing the whole patch.
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("ready", "后台服务已连接")
	var legacy := _settings()
	legacy["schema_version"] = 4
	legacy.erase("desktop")
	legacy["autonomy"].erase("cloud_enabled")
	window.populate(legacy, [])
	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "a legacy service still saves one patch")
	check(not patches[0].has("desktop"),
		"a legacy service receives no desktop group")
	check(not patches[0].autonomy.has("cloud_enabled"),
		"a legacy service receives no autonomy.cloud_enabled")
	check(patches[0].has("user_name"),
		"the rest of the legacy patch still saves user_name")
	check(window.desktop_enabled.disabled,
		"desktop controls stay locked for a legacy service")
	window.free()
	host.free()

class FakeService extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	var state := "ready"
	var features := {}
	var personas: Array = []
	var requests: Array = []
	var _sequence := 0

	func request(type: String, payload: Dictionary, timeout := 8.0) -> String:
		_sequence += 1
		var id := "fake-%d" % _sequence
		requests.append({"id": id, "type": type, "payload": payload, "timeout": timeout})
		return id

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)
