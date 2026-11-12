extends SceneTree
## Focused checks for the memory panel's "remember" and search entry points.

const MemoryPanel = preload("res://scripts/ui/memory_panel.gd")

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
	await _test_remember()
	await _test_search()
	await _test_empty_input()
	print("MEMORY_PANEL_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _features() -> Dictionary:
	return {
		"memory": {
			"available": true,
			"commands": [
				"memory.list", "memory.propose", "memory.search", "memory.correct",
				"memory.delete", "memory.get", "memory.history", "memory.archive",
				"memory.restore",
			],
		},
	}

func _test_remember() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := MemoryPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	check(fake.requests.size() == 1, "configure loads the memory list")
	fake.reply(fake.requests[0].id, "result", {"memories": []})
	check(not panel.remember_button.disabled, "remember button enables when the service is ready")
	panel.remember_input.text = "  喜欢猫  "
	panel.remember_button.pressed.emit()
	var propose = fake.requests[-1]
	check(propose.type == "memory.propose", "remember button sends memory.propose")
	check(propose.payload.get("text", "") == "喜欢猫", "propose carries the trimmed text")
	fake.reply(propose.id, "result", {"status": "committed", "memory": {"id": "m1", "text": "喜欢猫"}})
	check(panel.remember_input.text.is_empty(), "successful propose clears the input")
	var refresh_request = fake.requests[-1]
	check(refresh_request.type == "memory.list", "successful propose refreshes the list")
	fake.reply(refresh_request.id, "result", {"memories": [
		{"id": "m1", "text": "喜欢猫", "category": "general", "importance": 0.7, "status": "active"},
	]})
	check(panel.list_box.get_child_count() == 1, "refreshed list renders the new memory")
	check(panel.status.text.contains("1"), "list status reports the memory count")
	panel.remember_input.text = "重复的记忆"
	panel.remember_button.pressed.emit()
	fake.reply(fake.requests[-1].id, "result", {"status": "rejected", "reason": "memory_disabled"})
	check(panel.status.text.contains("memory_disabled"), "rejected propose shows the reason")
	panel.remember_input.text = "太长的记忆"
	panel.remember_button.pressed.emit()
	fake.reply(fake.requests[-1].id, "error", {"code": "invalid_request", "message": "记忆过长"})
	check(panel.status.text.contains("记忆过长"), "propose error shows the server message")
	panel.free()
	fake.free()

func _test_search() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := MemoryPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	fake.reply(fake.requests[0].id, "result", {"memories": []})
	panel.search_input.text = "猫"
	panel.search_button.pressed.emit()
	var search = fake.requests[-1]
	check(search.type == "memory.search", "search button sends memory.search")
	check(search.payload.get("query", "") == "猫", "search carries the keyword")
	fake.reply(search.id, "result", {
		"query": "猫",
		"count": 3,
		"results": [
			{"id": "m1", "text": "喜欢猫", "category": "general", "importance": 0.7, "status": "active", "score": 2},
			{"id": "m2", "text": "猫粮", "category": "life", "importance": 0.5, "status": "active", "score": 1},
			{"id": "m3", "text": "猫窝", "category": "life", "importance": 0.4, "status": "active", "score": 1},
		],
	})
	check(panel.search_box.get_child_count() == 3, "search results render one row per hit")
	check(panel.status.text.contains("3"), "search status reports the result count")
	check(not panel.list_scroll.visible, "search results replace the normal list")
	check(panel.search_scroll.visible, "search results area is visible")
	panel.search_input.text = ""
	panel.search_input.text_changed.emit("")
	check(panel.search_box.get_child_count() == 0, "clearing the query clears the results")
	check(panel.list_scroll.visible, "clearing the query returns to the normal list")
	check(not panel.search_scroll.visible, "search results area hides after clearing")
	panel.free()
	fake.free()

func _test_empty_input() -> void:
	var fake := FakeService.new()
	fake.features = _features()
	root.add_child(fake)
	var panel := MemoryPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	fake.reply(fake.requests[0].id, "result", {"memories": []})
	var before := fake.requests.size()
	panel.remember_input.text = "   "
	panel.remember_button.pressed.emit()
	panel.search_input.text = "   "
	panel.search_button.pressed.emit()
	check(fake.requests.size() == before, "empty input sends no request")
	check(panel.search_box.get_child_count() == 0, "empty search leaves the results area empty")
	panel.free()
	fake.free()

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
