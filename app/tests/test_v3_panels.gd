extends SceneTree

const ServiceClient = preload("res://scripts/service_client.gd")
const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
const MemoryPanel = preload("res://scripts/ui/memory_panel.gd")
const ExpressionPanel = preload("res://scripts/ui/expression_panel.gd")
const GrowthPanel = preload("res://scripts/ui/growth_panel.gd")
const UsagePanel = preload("res://scripts/ui/usage_panel.gd")

var checks := 0
var failures := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	call_deferred("_run")

func _pending_entry(type: String, token: String) -> Dictionary:
	return {
		"type": type,
		"token": token,
		"stream_id": "",
		"deadline": Time.get_ticks_msec() + 40000,
		"timeout_ms": 40000,
	}

func _features() -> Dictionary:
	return {
		"memory": {
			"available": true,
			"commands": [
				"memory.list", "memory.correct", "memory.delete", "memory.get",
				"memory.history", "memory.archive", "memory.restore", "memory.compact",
				"memory.jobs.status", "memory.conflicts.list", "memory.conflicts.resolve",
				"memory.index.rebuild", "memory.index.status",
			],
		},
		"expression": {
			"available": true,
			"commands": ["expression.phrases.list", "expression.phrases.set_enabled"],
		},
		"growth": {
			"available": true,
			"commands": [
				"growth.list", "growth.approve", "growth.reject", "growth.history",
				"growth.rollback", "personality.lock",
			],
		},
		"usage": {"available": true, "commands": ["usage.get"]},
		"diag": {"available": true, "commands": ["diag.export"]},
	}

func _run() -> void:
	_test_hello_new_commands()
	await _test_settings_tabs()
	await _test_usage_panel()
	await _test_memory_history()
	await _test_expression_phrases()
	await _test_growth_panel()
	print("V3_PANELS_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _test_hello_new_commands() -> void:
	var client := ServiceClient.new()
	client.pending["h1"] = _pending_entry("hello", "h1")
	client._dispatch({
		"id": "h1",
		"type": "ready",
		"payload": {
			"protocol_version": 1,
			"settings": {"schema_version": 4},
			"features": _features(),
			"personas": [],
			"warnings": [],
		},
	})
	var memory_commands: Array = client.feature("memory").get("commands", [])
	check(memory_commands.has("memory.get"), "hello keeps memory.get")
	check(memory_commands.has("memory.history"), "hello keeps memory.history")
	check(memory_commands.has("memory.archive"), "hello keeps memory.archive")
	check(memory_commands.has("memory.restore"), "hello keeps memory.restore")
	check(memory_commands.has("memory.conflicts.list"), "hello keeps memory.conflicts.list")
	check(memory_commands.has("memory.index.status"), "hello keeps memory.index.status")
	var growth_commands: Array = client.feature("growth").get("commands", [])
	check(growth_commands.has("personality.lock"), "hello keeps personality.lock")
	check(client.feature("expression").get("available", false), "hello advertises phrase editing")
	check(client.feature("usage").get("available", false), "hello advertises usage reporting")
	check(client.feature("diag").get("available", false), "hello advertises diag export")
	client.free()

func _test_settings_tabs() -> void:
	var window := SettingsWindow.new()
	window.visible = false
	root.add_child(window)
	await process_frame
	check(window.tabs != null, "settings window creates a TabContainer")
	check(window.tabs.get_tab_count() == 7, "settings window has seven tabs")
	var expected := ["基础", "AI", "记忆", "表达", "主动", "成长", "用量与诊断"]
	var titles: Array[String] = []
	for index in range(window.tabs.get_tab_count()):
		titles.append(window.tabs.get_tab_title(index))
	check(titles == expected, "settings tabs keep the requested order and labels")
	check(window.memory_panel != null, "memory tab hosts the memory panel")
	check(window.expression_panel != null, "expression tab hosts the expression panel")
	check(window.growth_panel != null, "growth tab hosts the growth panel")
	check(window.usage_panel != null, "usage tab hosts the usage panel")
	window.populate({
		"expression": {"style": {
			"casualness": 0.8,
			"warmth": 0.7,
			"directness": 0.6,
			"playfulness": 0.4,
			"sentence_length": 0.3,
			"emoji_frequency": 0.2,
		}},
		"growth": {"locked_traits": ["warmth"]},
	}, [])
	check(is_equal_approx(float(window.expression_panel.sliders["casualness"].value), 0.8), "populate restores expression style")
	check(window.growth_panel._lock_checks.has("warmth"), "populate restores locked traits")
	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "save still emits one settings patch")
	check(patches[0].has("expression"), "save patch includes expression style")
	check(is_equal_approx(float(patches[0].expression.style.casualness), 0.8), "save patch carries slider values")
	check(patches[0].has("memory") and patches[0].has("autonomy"), "save patch keeps memory and autonomy groups")
	window.free()

func _test_usage_panel() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := UsagePanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	var request = fake.requests[-1]
	check(request.type == "usage.get", "usage panel loads usage.get")
	fake.reply(request.id, "result", _usage_payload())
	check(panel.today_label.text.contains("1200"), "usage panel renders today tokens")
	check(panel.today_label.text.contains("0.03"), "usage panel renders today cost")
	check(panel.today_label.text.contains("4"), "usage panel renders today calls")
	check(panel.month_label.text.contains("34000"), "usage panel renders month tokens")
	check(panel.month_label.text.contains("1.25"), "usage panel renders month cost")
	check(panel.status_label.text.contains("接近上限"), "usage panel maps warning status")
	check(absf(panel.budget_bar.value - 0.083) < 0.001, "usage panel fills the cost budget bar")
	check(panel.price_label.text.contains("已配置"), "usage panel shows price configuration")
	check(panel.models_label.text.contains("deepseek-chat"), "usage panel lists models")
	check(panel.days_label.text.contains("2026-11-04"), "usage panel lists days")
	check(panel.open_button.disabled, "open folder starts disabled")
	panel._export_diag()
	var export_request = fake.requests[-1]
	check(export_request.type == "diag.export", "export button requests diag.export")
	fake.reply(export_request.id, "result", {
		"path": "C:/tmp/diag/bundle.zip",
		"bytes": 2048,
		"included": ["settings", "state"],
	})
	check(panel.path_label.text.contains("bundle.zip"), "export path is shown")
	check(panel.path_label.text.contains("2048"), "export size is shown")
	check(panel.included_label.text.contains("settings"), "export contents are shown")
	check(not panel.open_button.disabled, "open folder enables after export")
	check(panel.folder_path() == "C:/tmp/diag", "folder path is derived from the bundle path")
	panel.free()
	fake.free()

	var unavailable := FakeService.new()
	unavailable.features = {"usage": {"available": true}, "diag": {"available": true}}
	root.add_child(unavailable)
	var second := UsagePanel.new()
	root.add_child(second)
	await process_frame
	second.configure(unavailable)
	unavailable.reply(unavailable.requests[-1].id, "error", {"code": "not_available", "message": "未接入"})
	check(second.status_label.text.contains("未接入"), "unavailable usage.get shows the placeholder")
	check(second.refresh_button.disabled, "unavailable usage.get disables refresh")
	second._export_diag()
	unavailable.reply(unavailable.requests[-1].id, "error", {"code": "not_available", "message": "未接入"})
	check(second.export_button.disabled, "unavailable diag.export disables the button")
	second.free()
	unavailable.free()

func _test_memory_history() -> void:
	var fake := FakeService.new()
	fake.features = {"memory": {"available": true}}
	root.add_child(fake)
	var panel := MemoryPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	var list_request = fake.requests[-1]
	check(list_request.type == "memory.list", "memory panel loads memory.list")
	fake.reply(list_request.id, "result", {"memories": [
		{"id": "m1", "text": "喜欢猫", "category": "general", "importance": 0.7, "status": "active"},
		{"id": "m2", "text": "旧住址", "category": "life", "importance": 0.4, "status": "archived"},
	]})
	check(panel.list_box.get_child_count() == 2, "memory rows render body text")
	var row := panel.row_for("m1")
	check(row != null, "memory rows can be looked up by id")
	check(row.get_child(1) is Button and row.get_child(1).text == "删除", "each row keeps the delete button")
	var history_button: Button = row.get_meta("history_button")
	check(history_button != null and history_button.text == "历史", "each row has a history button")
	history_button.pressed.emit()
	var history_request = fake.requests[-1]
	check(history_request.type == "memory.history", "history button uses memory.history")
	check(history_request.payload.memory_id == "m1", "history request targets the memory id")
	fake.reply(history_request.id, "result", {"versions": [
		{"id": "v2", "text": "喜欢猫", "status": "active", "created_at": "2026-11-04", "source_type": "manual"},
		{"id": "v1", "text": "喜欢狗", "status": "superseded", "created_at": "2026-11-01", "source_type": "manual"},
	]})
	check(panel.history_list.get_child_count() == 2, "history dialog lists every version")
	check(panel.history_dialog.title.contains("m1"), "history dialog names the memory")
	var archived := panel.row_for("m2")
	var restore_button: Button = archived.get_meta("restore_button")
	check(restore_button != null and restore_button.text == "恢复", "archived rows expose restore")
	restore_button.pressed.emit()
	var restore_request = fake.requests[-1]
	check(restore_request.type == "memory.restore", "restore button uses memory.restore")
	check(restore_request.payload.memory_id == "m2", "restore targets the archived memory")
	panel._request_history("m1")
	fake.reply(fake.requests[-1].id, "error", {"code": "not_available", "message": "未接入"})
	check(panel.status.text.contains("未接入"), "unavailable history shows the placeholder")
	check(not panel._history_available, "unavailable history disables later calls")
	check(history_button.disabled, "unavailable history disables the row button")
	panel.free()
	fake.free()

func _test_expression_phrases() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := ExpressionPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	var request = fake.requests[-1]
	check(request.type == "expression.phrases.list", "expression panel loads phrase list")
	fake.reply(request.id, "result", {"phrases": [
		{"id": "p1", "text": "好呀", "category": "daily", "enabled": true, "cooldown_seconds": 30, "daily_limit": 5, "source": "builtin"},
		{"id": "p2", "text": "收到", "category": "work", "enabled": false, "cooldown_seconds": 60, "daily_limit": 3, "source": "builtin"},
	]})
	check(panel.phrase_checks.size() == 2, "phrase list renders one switch per phrase")
	var first: CheckButton = panel.phrase_checks[0]
	check(first.button_pressed, "phrase switches reflect enabled state")
	first.button_pressed = false
	var change = fake.requests[-1]
	check(change.type == "expression.phrases.set_enabled", "toggle uses expression.phrases.set_enabled")
	check(change.payload.phrase_id == "p1", "toggle sends the phrase id")
	check(change.payload.enabled == false, "toggle sends the new enabled state")
	fake.reply(change.id, "result", {"saved": true, "phrase_id": "p1", "enabled": false})
	check(panel.phrases_status.text.contains("已保存"), "saved phrase switch reports success")
	panel._apply_preset("customer")
	check(float(panel.style_values().casualness) > 0.5, "customer preset raises casualness")
	panel._apply_preset("noisy")
	check(float(panel.style_values().emoji_frequency) < 0.5, "noisy preset lowers emoji frequency")
	panel._apply_preset("default")
	check(is_equal_approx(float(panel.style_values().playfulness), 0.5), "default preset restores 0.5")
	var second: CheckButton = panel.phrase_checks[1]
	second.button_pressed = true
	fake.reply(fake.requests[-1].id, "error", {"code": "not_available", "message": "未接入"})
	check(panel.phrases_status.text.contains("未接入"), "unavailable phrase command shows the placeholder")
	check(panel.refresh_button.disabled, "unavailable phrase command disables refresh")
	panel.free()
	fake.free()

func _test_growth_panel() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := GrowthPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	var request = fake.requests[-1]
	check(request.type == "growth.list", "growth panel loads growth.list")
	var payload := {
		"candidates": [{
			"id": "g1", "trait": "warmth", "delta": 0.05,
			"evidence_ids": ["m1", "m2"], "status": "candidate", "created_at": "2026-11-04",
		}],
		"history": [{
			"id": "h1", "trait": "warmth", "old_value": 0.4, "new_value": 0.45,
			"created_at": "2026-11-03", "reverted_at": "",
		}],
	}
	fake.reply(request.id, "result", payload)
	check(panel.candidates_box.get_child_count() == 1, "growth candidates render")
	check(panel.history_box.get_child_count() == 1, "growth history renders")
	check(panel._lock_checks.has("warmth"), "growth candidates create a lock switch")
	var candidate_row := panel.candidates_box.get_child(0)
	var approve: Button = candidate_row.get_child(1)
	check(approve.text == "采纳", "candidate row has an approve button")
	approve.pressed.emit()
	var approve_request = fake.requests[-1]
	check(approve_request.type == "growth.approve", "approve uses growth.approve")
	check(approve_request.payload.candidate_id == "g1", "approve targets the candidate")
	fake.reply(approve_request.id, "result", {"status": "applied", "trait": "warmth", "old_value": 0.4, "new_value": 0.45, "history_id": "h2"})
	check(fake.requests[-1].type == "growth.list", "approve refreshes the growth list")
	fake.reply(fake.requests[-1].id, "result", payload)
	var history_row := panel.history_box.get_child(0)
	var rollback: Button = history_row.get_child(1)
	check(rollback.text == "回滚", "history row has a rollback button")
	rollback.pressed.emit()
	var rollback_request = fake.requests[-1]
	check(rollback_request.type == "growth.rollback", "rollback uses growth.rollback")
	check(rollback_request.payload.history_id == "h1", "rollback targets the history entry")
	var lock: CheckButton = panel._lock_checks["warmth"]
	lock.button_pressed = true
	var lock_request = fake.requests[-1]
	check(lock_request.type == "personality.lock", "lock switch uses personality.lock")
	check(lock_request.payload.trait == "warmth", "lock switch sends the trait")
	check(lock_request.payload.locked == true, "lock switch sends the locked state")
	fake.reply(lock_request.id, "result", {"trait": "warmth", "locked": true})
	panel.free()
	fake.free()

	var unavailable := FakeService.new()
	unavailable.features = {"growth": {"available": false}}
	root.add_child(unavailable)
	var second := GrowthPanel.new()
	root.add_child(second)
	await process_frame
	second.configure(unavailable)
	check(second.status.text.contains("未接入"), "unavailable growth shows the placeholder")
	check(second.refresh_button.disabled, "unavailable growth disables refresh")
	second.free()
	unavailable.free()

func _usage_payload() -> Dictionary:
	return {
		"today": {"tokens": 1200, "cost": 0.03, "calls": 4},
		"month": {"tokens": 34000, "cost": 1.25, "calls": 22},
		"limits": {"daily_tokens": 120000, "monthly_cost": 15.0, "currency": "USD"},
		"ratio": {"tokens": 0.1, "cost": 0.083},
		"status": "warning",
		"by_model": [{"model": "deepseek-chat", "tokens": 34000, "cost": 1.25, "calls": 22}],
		"by_day": [{"date": "2026-11-04", "tokens": 1200, "cost": 0.03, "calls": 4}],
		"price_configured": true,
	}

class FakeService extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	var state := "ready"
	var features := {"memory": {"available": true}}
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

	func event(id: String, type: String, payload: Dictionary) -> void:
		event_received.emit(id, type, payload)
