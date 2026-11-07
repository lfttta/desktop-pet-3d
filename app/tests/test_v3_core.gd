extends SceneTree

const ServiceClient = preload("res://scripts/service_client.gd")
const ChatSession = preload("res://scripts/chat/chat_session.gd")
const ChatWindow = preload("res://scripts/ui/chat_window.gd")
const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
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

func _pending_entry(type: String, token: String) -> Dictionary:
	return {
		"type": type,
		"token": token,
		"stream_id": "",
		"deadline": Time.get_ticks_msec() + 40000,
		"timeout_ms": 40000,
	}

func _run() -> void:
	_test_client_dispatch()
	_test_chat_session()
	await _test_chat_window()
	await _test_settings_window()
	await _test_memory_panel()
	print("V3_CORE_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _test_client_dispatch() -> void:
	var client := ServiceClient.new()
	var handshakes: Array = []
	var finished: Array = []
	var events: Array = []
	client.capabilities_received.connect(func(features: Dictionary, personas: Array):
		handshakes.append({"features": features, "personas": personas}))
	client.request_finished.connect(func(id: String, type: String, _payload: Dictionary):
		finished.append({"id": id, "type": type}))
	client.event_received.connect(func(_id: String, type: String, _payload: Dictionary):
		events.append(type))

	client.pending["h1"] = _pending_entry("hello", "h1")
	client._dispatch({
		"id": "h1",
		"type": "ready",
		"payload": {
			"protocol_version": 1,
			"settings": {"schema_version": 3},
			"features": {"chat": {"available": true, "commands": ["chat.send", "chat.cancel", "chat.history"]}},
			"personas": [{"id": "godette", "display_name": "Godette"}],
			"warnings": [],
		},
	})
	check(handshakes.size() == 1, "hello emits capabilities_received")
	var commands = handshakes[0].features.chat.commands
	check(commands.size() == 3 and commands[0] == "chat.send"
		and commands[1] == "chat.cancel" and commands[2] == "chat.history",
		"hello keeps the chat command table")
	check(handshakes[0].personas[0].id == "godette", "hello carries the persona catalog")
	check(client.features.chat.available, "hello stores features for lookups")
	check(client.feature("chat").get("available", false), "feature lookup returns the capability")
	check(not client.pending.has("h1"), "hello completes the transport request")

	client.pending["c1"] = _pending_entry("chat.send", "c1")
	var started := Time.get_ticks_msec()
	client._dispatch({"id": "c1", "type": "chat.delta", "payload": {"text": "你"}})
	check(events.size() == 1 and events[0] == "chat.delta", "chat.delta reaches the event signal")
	check(client.pending.has("c1"), "a delta keeps the request open")
	check(int(client.pending["c1"].deadline) > started + 30000, "a delta renews the stream deadline")
	client._dispatch({"id": "c1", "type": "chat.done", "payload": {"message": {"content": "你好"}}})
	check(events[-1] == "chat.done", "chat.done reaches the event signal")
	check(not client.pending.has("c1"), "chat.done closes the request")
	check(finished[-1].type == "chat.done", "chat.done also reports completion")

	client.pending["c2"] = _pending_entry("chat.send", "c2")
	client._dispatch({"id": "c2", "type": "chat.error", "payload": {"code": "rate_limited", "message": "太快", "retryable": true}})
	check(events[-1] == "chat.error", "chat.error reaches the event signal")
	check(not client.pending.has("c2"), "chat.error closes the request")

	client.pending["c3"] = _pending_entry("chat.send", "c3")
	client._dispatch({"id": "c3", "type": "accepted", "payload": {"request_id": "s3"}})
	check(events[-1] == "accepted", "accepted reaches the event signal")
	check(client.pending.has("c3") and client.pending.has("s3"), "accepted request_id keeps the stream open under both ids")
	client._dispatch({"id": "s3", "type": "chat.delta", "payload": {"text": "续"}})
	check(events[-1] == "chat.delta", "stream events can arrive under the accepted id")
	client._dispatch({"id": "s3", "type": "chat.done", "payload": {"message": "完成"}})
	check(not client.pending.has("s3") and not client.pending.has("c3"), "terminal event clears both ids")

	client.pending["c4"] = _pending_entry("chat.send", "c4")
	client._dispatch({"id": "c4", "type": "result", "payload": {"request_id": "s4"}})
	check(client.pending.has("s4"), "a result-shaped accepted payload also aliases the request")
	client._dispatch({"id": "s4", "type": "chat.error", "payload": {"code": "internal_error", "message": "坏了", "retryable": true}})
	check(not client.pending.has("s4") and not client.pending.has("c4"), "an aliased error clears the request")

	# A slow periodic poll must fail locally instead of restarting the backend.
	var poll_results: Array = []
	client.request_finished.connect(func(_id: String, type: String, payload: Dictionary):
		poll_results.append({"type": type, "payload": payload}))
	client.pending["p1"] = _pending_entry("state.get", "p1")
	client.pending["p1"]["deadline"] = 0
	client._expire_requests(Time.get_ticks_msec())
	check(not client.pending.has("p1"), "an expired state.get is dropped")
	check(poll_results.size() == 1 and poll_results[0].payload.get("code") == "timeout",
		"an expired state.get reports a local timeout")
	check(client.state == "ready",
		"an expired state.get does not tear down the transport")
	client.free()

func _test_chat_session() -> void:
	var fake := FakeService.new()
	var session := ChatSession.new()
	session.configure(fake)
	var deltas: Array = []
	var replies: Array = []
	var completions: Array = []
	var errors: Array = []
	var unavailable: Array = []
	var cancelled: Array = []
	var history: Array = []
	var history_errors: Array = []
	session.delta_received.connect(func(text: String): deltas.append(text))
	session.assistant_message.connect(func(text: String): replies.append(text))
	session.completed.connect(func(message: String, _usage: Dictionary, _ops: Array, _state: Dictionary): completions.append(message))
	session.error_received.connect(func(_code: String, _message: String, retryable: bool): errors.append(retryable))
	session.unavailable.connect(func(message: String): unavailable.append(message))
	session.cancelled.connect(func(): cancelled.append(true))
	session.history_loaded.connect(func(items: Array): history.append(items))
	session.history_unavailable.connect(func(message: String): history_errors.append(message))

	check(session.is_available(), "chat capability enables the session")
	var id := session.send("你好")
	check(id == "fake-1", "send returns the transport id")
	check(fake.requests[0].type == "chat.send", "send uses chat.send")
	check(fake.requests[0].payload.text == "你好", "send carries the text")
	check(fake.requests[0].timeout == 40.0, "chat.send uses the streaming timeout")
	fake.event(id, "accepted", {"request_id": "stream-1"})
	check(session.active_request_id() == "stream-1", "accepted switches to the stream request id")
	fake.event("stream-1", "chat.delta", {"text": "第一"})
	fake.event("stream-1", "chat.delta", {"text": "第二"})
	check(deltas.size() == 2 and deltas[0] == "第一" and deltas[1] == "第二", "deltas accumulate in order")
	fake.event("stream-1", "chat.done", {
		"message": {"content": "第一第二"},
		"usage": {"total_tokens": 12},
		"memory_ops": [{"op": "propose"}],
		"state_delta": {"mood": "happy"},
	})
	check(replies.size() == 1 and replies[0] == "第一第二", "done emits one final assistant message")
	check(completions.size() == 1, "done emits completion details")
	check(not session.is_streaming(), "done ends the stream")

	var cancel_id := session.send("再来")
	fake.event(cancel_id, "chat.delta", {"text": "半"})
	var cancel_transport := session.cancel()
	check(cancel_transport == "fake-3", "cancel returns the cancel transport id")
	check(fake.requests[-1].type == "chat.cancel", "cancel uses chat.cancel")
	check(fake.requests[-1].payload.request_id == cancel_id, "cancel targets the active request")
	check(cancelled.size() == 1, "cancel emits the cancelled signal")
	check(not session.is_streaming(), "cancel ends the stream")
	fake.event(cancel_id, "chat.delta", {"text": "忽略"})
	check(deltas[-1] == "半", "late deltas after cancel are ignored")

	var stream_cancel_id := session.send("流式取消")
	fake.event(stream_cancel_id, "accepted", {
		"request_id": "stream-2",
		"conversation_id": "c-9",
	})
	check(session.conversation_id() == "c-9", "accepted binds the conversation id")
	check(session.active_request_id() == "stream-2", "accepted stream id becomes active")
	var stream_cancel_transport := session.cancel()
	check(fake.requests[-1].type == "chat.cancel", "stream cancel uses chat.cancel")
	check(fake.requests[-1].payload.request_id == "stream-2",
		"stream cancel targets the accepted request id")
	check(stream_cancel_transport != "", "stream cancel returns a transport id")
	var delta_count := deltas.size()
	fake.event("stream-2", "chat.delta", {"text": "迟到的内容"})
	check(deltas.size() == delta_count, "late deltas under the stream id are ignored")
	var follow_id := session.send("继续")
	check(follow_id != "", "a follow-up message can be sent after cancel")
	check(fake.requests[-1].payload.conversation_id == "c-9",
		"a follow-up message keeps the cancelled conversation")
	fake.event(follow_id, "chat.done", {"message": {"content": "继续回复"}})
	check(replies[-1] == "继续回复", "a follow-up reply completes normally")

	var retry_id := session.send("失败")
	fake.reply(retry_id, "error", {"code": "rate_limited", "message": "太快", "retryable": true})
	check(errors.size() == 1 and errors[0], "retryable errors keep the retry flag")
	var unavailable_id := session.send("未接入")
	fake.reply(unavailable_id, "error", {"code": "not_available", "message": "未接入"})
	check(unavailable.size() == 1, "not_available maps to the unavailable signal")

	var one_shot := session.send("旧协议")
	fake.reply(one_shot, "result", {"message": "一次性"})
	check(replies[-1] == "一次性", "one-shot results still produce an assistant message")

	var history_id := session.load_history(20)
	check(fake.requests[-1].type == "chat.history", "load_history uses chat.history")
	check(fake.requests[-1].payload.limit == 20, "load_history forwards the limit")
	fake.reply(history_id, "result", {
		"messages": [{"role": "user", "content": "旧消息"}, {"role": "assistant", "text": "旧回复"}],
		"conversation_id": "c9",
	})
	check(history.size() == 1 and history[0].size() == 2, "history loads normalized messages")
	check(history[0][1].text == "旧回复", "history keeps assistant text")
	check(session.conversation_id() == "c9", "history stores the conversation id")
	var missing_id := session.load_history()
	fake.reply(missing_id, "error", {"code": "not_available", "message": "历史未接入"})
	check(history_errors.size() == 1, "unavailable history maps to its own signal")
	session.configure(null)
	fake.free()

func _test_chat_window() -> void:
	var fake := FakeService.new()
	fake.personas = [{"id": "godette", "display_name": "Godette"}]
	root.add_child(fake)
	var window := ChatWindow.new()
	window.visible = false
	root.add_child(window)
	await process_frame
	window.configure_chat(fake, {"persona_id": "godette"})
	fake.capabilities_received.emit(fake.features, fake.personas)
	await process_frame
	check(window.input.editable and not window.send_button.disabled, "capability enables the chat input")
	check(window.stop_button.disabled, "stop starts disabled")

	window.input.text = "你好"
	window._send()
	var sent = fake.requests[-1]
	check(sent.type == "chat.send", "window sends chat.send")
	check(window.stop_button.disabled == false, "stop is enabled while streaming")
	check(window.input.editable == false, "input is locked while streaming")
	fake.event(sent.id, "chat.delta", {"text": "你"})
	fake.event(sent.id, "chat.delta", {"text": "好"})
	var streamed := window.messages.get_child(window.messages.get_child_count() - 1) as Label
	check(streamed != null and streamed.text.contains("你好"), "deltas render into one assistant label")
	fake.event(sent.id, "chat.done", {"message": {"content": "你好"}, "usage": {"total_tokens": 3}})
	check(window.stop_button.disabled, "stop disables after completion")
	check(window.input.editable, "input unlocks after completion")
	check(window.status.text.contains("3"), "completion shows token usage")

	window.input.text = "停止测试"
	window._send()
	var second = fake.requests[-1]
	window._stop()
	check(fake.requests[-1].type == "chat.cancel", "stop uses chat.cancel")
	check(fake.requests[-1].payload.request_id == second.id, "stop targets the active request")
	check(window.status.text.contains("已停止"), "stop renders a status message")
	check(window.stop_button.disabled, "stop disables after cancellation")

	fake.features = {"chat": {"available": true}}
	var history_id: String = window._session.load_history()
	fake.reply(history_id, "result", {"messages": [
		{"role": "user", "content": "旧问题"},
		{"role": "assistant", "content": "旧回答"},
	]})
	check(window.messages.get_child_count() == 3, "history replaces the message list")
	window.free()
	fake.free()

func _test_settings_window() -> void:
	var window := SettingsWindow.new()
	window.visible = false
	root.add_child(window)
	await process_frame
	window.populate({}, [])
	check(window.ai_base_url.text == "https://api.deepseek.com", "AI base_url defaults to DeepSeek")
	check(window.ai_model.text == "deepseek-chat", "AI model defaults to deepseek-chat")
	check(is_equal_approx(window.ai_temperature.value, 0.8), "AI temperature default is 0.8")
	check(window.ai_connect_timeout.value == 5000, "connect timeout default is 5000 ms")
	check(window.ai_request_timeout.value == 30000, "request timeout default is 30000 ms")
	check(window.ai_daily_tokens.value == 120000, "daily token budget default is 120000")
	check(is_equal_approx(window.ai_monthly_budget.value, 15.0), "monthly cost budget default is 15")
	check(window.memory_enabled.button_pressed, "memory defaults to enabled")
	check(not window.memory_auto_capture.button_pressed, "auto capture defaults to off")
	check(not window.autonomy_enabled.button_pressed, "autonomy defaults to off")
	check(window.quiet_start_input.text == "23:00", "quiet start defaults to 23:00")
	check(window.quiet_end_input.text == "08:00", "quiet end defaults to 08:00")
	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "save emits one settings.update patch")
	for group in ["ai", "memory", "autonomy"]:
		check(patches[0].has(group), "save patch includes the %s group" % group)
	check(patches[0].ai.model == "deepseek-chat", "save patch preserves AI defaults")
	check(patches[0].autonomy.quiet_start == "23:00", "save patch preserves quiet hours")
	check(not patches[0].has("character_id"), "save patch leaves the character untouched")
	window.free()

	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	fake.features = {"memory": {"available": false}, "autonomy": {"available": false}}
	host.add_child(fake)
	var bound := SettingsWindow.new()
	bound.visible = false
	host.add_child(bound)
	await process_frame
	bound.show_status("ready", "后台服务已连接")
	bound.populate({}, [])
	check(bound.ai_key_button.disabled == false, "AI key controls start enabled when the service is ready")
	check(bound.quiet_button.disabled, "autonomy command stays disabled while unavailable")
	bound.ai_key.text = "dummy-key"
	bound._save_key()
	check(fake.requests[-1].type == "ai.set_key", "key save uses ai.set_key")
	check(fake.requests[-1].payload.key == "dummy-key", "key value stays in the request payload")
	check(bound.ai_key.text.is_empty(), "key field clears after submitting")
	fake.reply(fake.requests[-1].id, "error", {"code": "not_available", "message": "未接入"})
	check(bound.ai_key_status.text.contains("未接入"), "unavailable AI reports a placeholder")
	check(bound.ai_key_button.disabled, "unavailable AI disables the key controls")
	bound._set_quiet()
	check(fake.requests[-1].type == "autonomy.set_quiet", "manual quiet uses autonomy.set_quiet")
	check(fake.requests[-1].payload.minutes == 30, "manual quiet requests 30 minutes")
	bound.free()
	host.free()

func _test_memory_panel() -> void:
	var fake := FakeService.new()
	fake.features = {"memory": {"available": false}}
	root.add_child(fake)
	var panel := MemoryPanel.new()
	root.add_child(panel)
	await process_frame
	panel.configure(fake)
	check(panel.status.text.contains("未接入"), "memory panel shows the placeholder while unavailable")
	check(panel.refresh_button.disabled, "memory refresh is disabled while unavailable")
	fake.features = {"memory": {"available": true}}
	fake.capabilities_received.emit(fake.features, [])
	check(not panel.refresh_button.disabled, "capability enables memory refresh")
	panel.refresh()
	var list_request = fake.requests[-1]
	check(list_request.type == "memory.list", "refresh uses memory.list")
	fake.reply(list_request.id, "result", {"memories": [
		{"id": "m1", "text": "喜欢猫", "category": "general"},
	]})
	check(panel.list_box.get_child_count() == 1, "memory rows render from memory.list")
	var row := panel.list_box.get_child(0)
	var remove := row.get_child(1) as Button
	check(remove != null and remove.text == "删除", "each memory row has a delete button")
	remove.pressed.emit()
	check(fake.requests[-1].type == "memory.delete", "delete uses memory.delete")
	check(fake.requests[-1].payload.memory_id == "m1", "delete targets the memory id")
	fake.reply(fake.requests[-1].id, "result", {"deleted": true})
	check(fake.requests[-1].type == "memory.list", "a successful delete refreshes the list")
	panel.free()
	fake.queue_free()

class FakeService extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	var state := "ready"
	var features := {"chat": {"available": true, "commands": ["chat.send", "chat.cancel", "chat.history"]}}
	var personas: Array = []
	var requests: Array = []
	var _sequence := 0

	func request(type: String, payload: Dictionary, timeout := 8.0) -> String:
		_sequence += 1
		var id := "fake-%d" % _sequence
		requests.append({"id": id, "type": type, "payload": payload, "timeout": timeout})
		return id

	func cancel(id: String) -> String:
		_sequence += 1
		var cancel_id := "fake-%d" % _sequence
		requests.append({"id": cancel_id, "type": "chat.cancel", "payload": {"request_id": id}, "timeout": 8.0})
		return cancel_id

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)

	func event(id: String, type: String, payload: Dictionary) -> void:
		event_received.emit(id, type, payload)
