extends SceneTree
## Desktop sensing controls on the autonomy page: backfill, save patch and the
## disabled state while the service is down.

const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
const PetWindow = preload("res://scripts/pet_window.gd")

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
	await _test_behavior_performance_settings()
	await _test_behavior_legacy_schema()
	await _test_bubble_greeting_settings()
	_test_runtime_settings()
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
	# Keys that gained controls in this pass: each must actually travel, and the
	# defaults must keep speaking and prompt logging off.
	check(patch.has("voice") and patch.voice.enabled == false,
		"voice stays off by default and the switch travels in the patch")
	check(patch.has("privacy") and patch.privacy.log_prompts == false,
		"prompt logging is off by default and travels in the patch")
	check(int(patch.ai.max_tokens) == 800, "max_tokens round-trips into the patch")
	check(int(patch.ai.max_retries) == 1, "max_retries round-trips into the patch")
	# Keys that used to exist only in the schema. Each one now has a control and
	# must still travel; a control that never saves is the same lie as no control.
	check(str(patch.ai.provider) == "deepseek", "ai.provider travels in the patch")
	check(str(patch.ai.chat_path) == "/v1/chat/completions", "ai.chat_path travels in the patch")
	check(str(patch.ai.currency) == "USD", "ai.currency travels in the patch")
	check(int(patch.memory.max_injected) == 5, "memory.max_injected travels in the patch")
	check(int(patch.memory.max_entry_chars) == 500, "memory.max_entry_chars travels in the patch")
	check(patch.expression.enabled == true and patch.expression.phrases_enabled == true,
		"expression switches travel in the patch")
	check(int(patch.autonomy.min_seconds_between) == 1800,
		"autonomy.min_seconds_between travels in the patch")
	check(int(patch.autonomy.max_per_hour) == 2, "autonomy.max_per_hour travels in the patch")

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


## Schema 6 behavior/performance keys: populate restores them, out-of-range
## values clamp into the spin boxes, and save sends them back.
func _test_behavior_performance_settings() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("ready", "后台服务已连接")
	var settings := _settings()
	settings["schema_version"] = 6
	settings["behavior"] = {"enabled": false}
	settings["performance"] = {"idle_fps": 45, "drag_fps": 90, "hidden_fps": 10}
	settings["autonomy"]["default_quiet_minutes"] = 60
	window.populate(settings, [])
	await process_frame

	check(window.behavior_enabled != null, "basic page has a behavior switch")
	check(window.idle_fps != null and window.drag_fps != null and window.hidden_fps != null,
		"basic page has the three frame rate inputs")
	check(window.idle_fps.min_value == 10 and window.idle_fps.max_value == 60,
		"idle fps covers the service range 10 to 60")
	check(window.drag_fps.min_value == 30 and window.drag_fps.max_value == 120,
		"drag fps covers the service range 30 to 120")
	check(window.hidden_fps.min_value == 1 and window.hidden_fps.max_value == 15,
		"hidden fps covers the service range 1 to 15")
	check(not window.behavior_enabled.disabled, "a v6 service unlocks the behavior switch")
	check(window.idle_fps.editable and window.drag_fps.editable and window.hidden_fps.editable,
		"a v6 service unlocks the frame rate inputs")
	check(not window.behavior_enabled.button_pressed,
		"populate restores behavior.enabled")
	check(window.idle_fps.value == 45 and window.drag_fps.value == 90 and window.hidden_fps.value == 10,
		"populate restores performance idle/drag/hidden fps")
	check(window._quiet_minutes_value() == 60,
		"populate restores autonomy.default_quiet_minutes")
	var choices: Array = []
	for index in range(window.quiet_minutes.item_count):
		choices.append(int(window.quiet_minutes.get_item_metadata(index)))
	check(choices.has(15) and choices.has(30) and choices.has(60) and choices.has(120),
		"quiet duration offers 15/30/60/120, got %s" % [choices])

	# Out-of-range saved values clamp into the spin boxes instead of being kept.
	settings["performance"] = {"idle_fps": 999, "drag_fps": 1, "hidden_fps": 99}
	settings["autonomy"]["default_quiet_minutes"] = 999
	window.populate(settings, [])
	await process_frame
	check(window.idle_fps.value == 60, "an oversized idle fps clamps to 60")
	check(window.drag_fps.value == 30, "an undersized drag fps clamps to 30")
	check(window.hidden_fps.value == 15, "an oversized hidden fps clamps to 15")
	check(window._quiet_minutes_value() == 240,
		"an oversized quiet duration clamps to the service maximum")

	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "a v6 save emits one patch")
	var patch: Dictionary = patches[0]
	check(patch.has("behavior") and patch.behavior.enabled == false,
		"save patch carries behavior.enabled")
	check(patch.has("performance"), "save patch includes the performance group")
	check(int(patch.performance.idle_fps) == 60 and int(patch.performance.drag_fps) == 30
		and int(patch.performance.hidden_fps) == 15,
		"save patch carries the clamped frame rates")
	check(int(patch.autonomy.default_quiet_minutes) == 240,
		"save patch carries autonomy.default_quiet_minutes")

	# The quiet button now uses the selected duration instead of a fixed 30.
	window._select_quiet_minutes(120)
	window._set_quiet()
	var quiet_request: Dictionary = fake.requests[fake.requests.size() - 1]
	check(quiet_request.type == "autonomy.set_quiet",
		"the quiet button sends autonomy.set_quiet")
	check(int(quiet_request.payload.minutes) == 120,
		"the quiet action uses the selected duration, got %s" % [quiet_request.payload.minutes])

	window.free()
	host.free()


## A schema 5 service knows neither new group, so save must omit them.
func _test_behavior_legacy_schema() -> void:
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
	check(window.behavior_enabled.disabled, "a v5 service leaves the behavior switch locked")
	check(not window.idle_fps.editable and not window.drag_fps.editable
		and not window.hidden_fps.editable,
		"a v5 service leaves the frame rate inputs locked")
	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "a v5 save still emits one patch")
	check(not patches[0].has("behavior"), "a v5 service receives no behavior group")
	check(not patches[0].has("performance"), "a v5 service receives no performance group")
	check(not patches[0].autonomy.has("default_quiet_minutes"),
		"a v5 service receives no autonomy.default_quiet_minutes")
	check(not patches[0].has("bubble") and not patches[0].has("greeting"),
		"a v5 service receives neither the bubble nor the greeting group")
	check(window.bubble_enabled.disabled and window.greeting_enabled.disabled,
		"a v5 service leaves the bubble and greeting switches locked")
	window.free()
	host.free()


## Schema 7 bubble/greeting controls: their defaults, the backfilled values,
## the service ranges, and all six keys in the save patch.
func _test_bubble_greeting_settings() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("ready", "后台服务已连接")

	# A settings document without the two groups must fall back to the service
	# defaults, and those defaults must be what the save sends.
	var settings := _settings()
	settings["schema_version"] = 7
	window.populate(settings, [])
	await process_frame
	check(window.bubble_enabled != null, "autonomy page has a bubble switch")
	check(window.bubble_duration != null, "autonomy page has a bubble duration spin box")
	check(window.bubble_show_replies != null, "autonomy page has a reply bubble switch")
	check(window.greeting_enabled != null, "autonomy page has a greeting switch")
	check(window.greeting_on_start != null, "autonomy page has a start greeting switch")
	check(window.greeting_cooldown != null, "autonomy page has a greeting cooldown spin box")
	check(window.bubble_duration.min_value == 2 and window.bubble_duration.max_value == 15,
		"bubble duration covers the service range 2 to 15 seconds")
	check(window.greeting_cooldown.min_value == 5 and window.greeting_cooldown.max_value == 1440,
		"greeting cooldown covers the service range 5 to 1440 minutes")
	check(not window.bubble_enabled.disabled and window.bubble_duration.editable,
		"a v7 service unlocks the bubble controls")
	check(not window.greeting_enabled.disabled and window.greeting_cooldown.editable,
		"a v7 service unlocks the greeting controls")
	check(window.bubble_enabled.button_pressed, "bubble is on by default")
	check(window.bubble_duration.value == 4, "bubble duration defaults to 4 seconds")
	check(window.bubble_show_replies.button_pressed, "chat replies show in the bubble by default")
	check(window.greeting_enabled.button_pressed, "greeting is on by default")
	check(window.greeting_on_start.button_pressed, "a start greeting is on by default")
	check(window.greeting_cooldown.value == 60, "greeting cooldown defaults to 60 minutes")

	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "a v7 save emits one patch")
	var patch: Dictionary = patches[0]
	check(patch.has("bubble") and patch.has("greeting"),
		"save patch includes the bubble and greeting groups")
	check(patch.bubble.enabled == true and patch.bubble.show_replies == true,
		"the default bubble switches travel in the patch")
	check(int(patch.bubble.duration_seconds) == 4,
		"the default bubble duration travels in the patch")
	check(patch.greeting.enabled == true and patch.greeting.on_start == true,
		"the default greeting switches travel in the patch")
	check(int(patch.greeting.cooldown_minutes) == 60,
		"the default greeting cooldown travels in the patch")

	# Stored values must come back, and an out-of-range value clamps into the
	# spin box instead of reaching the service and being rejected.
	settings["bubble"] = {"enabled": false, "duration_seconds": 99, "show_replies": false}
	settings["greeting"] = {"enabled": false, "on_start": false, "cooldown_minutes": 1}
	window.populate(settings, [])
	await process_frame
	check(not window.bubble_enabled.button_pressed, "populate restores bubble.enabled")
	check(not window.bubble_show_replies.button_pressed,
		"populate restores bubble.show_replies")
	check(window.bubble_duration.value == 15, "an oversized bubble duration clamps to 15")
	check(not window.greeting_enabled.button_pressed, "populate restores greeting.enabled")
	check(not window.greeting_on_start.button_pressed, "populate restores greeting.on_start")
	check(window.greeting_cooldown.value == 5, "an undersized greeting cooldown clamps to 5")

	window.bubble_duration.value = 12
	window.greeting_cooldown.value = 240
	patches.clear()
	window._save()
	check(patches.size() == 1, "a second v7 save emits one patch")
	patch = patches[0]
	check(patch.bubble.enabled == false and patch.bubble.show_replies == false,
		"save patch carries the edited bubble switches")
	check(int(patch.bubble.duration_seconds) == 12, "save patch carries the bubble duration")
	check(patch.greeting.enabled == false and patch.greeting.on_start == false,
		"save patch carries the edited greeting switches")
	check(int(patch.greeting.cooldown_minutes) == 240,
		"save patch carries the greeting cooldown")

	window.free()
	host.free()


## pet_window.apply_runtime_settings is the entry point main.gd calls with the
## settings document; it must read every key without a running scene.
func _test_runtime_settings() -> void:
	var window := PetWindow.new()
	window.apply_runtime_settings({
		"behavior": {"enabled": false},
		"performance": {"idle_fps": 20, "drag_fps": 45, "hidden_fps": 3},
	})
	check(not window.behavior_enabled, "apply_runtime_settings turns the behavior chain off")
	check(window.idle_fps == 20 and window.drag_fps == 45 and window.hidden_fps == 3,
		"apply_runtime_settings reads the three frame caps")
	check(Engine.max_fps == 20, "the idle frame cap reaches Engine.max_fps")

	window.hidden = true
	window.apply_runtime_settings({})
	check(Engine.max_fps == 3, "the hidden frame cap applies while hidden")
	window.hidden = false

	window.apply_runtime_settings({
		"performance": {"idle_fps": 999, "drag_fps": 1, "hidden_fps": 0},
	})
	check(window.idle_fps == 20 and window.drag_fps == 45 and window.hidden_fps == 3,
		"out-of-range frame caps keep the previous values")
	check(not window.behavior_enabled,
		"a missing behavior group leaves the current switch alone")

	# --no-behavior still wins over a saved true.
	var forced := PetWindow.new()
	forced._behavior_forced_off = true
	forced.behavior_enabled = false
	forced.apply_runtime_settings({"behavior": {"enabled": true}})
	check(not forced.behavior_enabled, "--no-behavior wins over behavior.enabled")
	Engine.max_fps = 30
	window.free()
	forced.free()

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
