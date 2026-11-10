extends SceneTree

const ChatWindow = preload("res://scripts/ui/chat_window.gd")
const ChatSession = preload("res://scripts/chat/chat_session.gd")

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
	var fake := FakeClient.new()
	fake.personas = [{"id": "godette", "display_name": "Godette"}]
	var window: Window = ChatWindow.new()
	window.visible = false
	root.add_child(window)
	await process_frame
	check(window.get_child_count() > 0 and window.get_child(0) is ColorRect, "chat window keeps an opaque background")
	check(not window.transparent and not window.transparent_bg, "chat window disables native transparency")
	check(window.persona_label.text.contains("Godette"), "persona label shows the current persona")
	check(not window.input.editable, "input starts disabled before capabilities arrive")
	check(window.send_button.disabled, "send starts disabled before capabilities arrive")
	check(window.empty_state.text.contains("尚未接入"), "empty state names the unimplemented feature")
	window.configure_chat(fake, {"persona_id": "godette"})
	await process_frame
	check(not window.input.editable and window.send_button.disabled, "unavailable chat stays disabled")
	fake.features = {"chat": {"available": true, "commands": ["chat.send"]}}
	fake.capabilities_received.emit(fake.features, fake.personas)
	check(window.input.editable and not window.send_button.disabled, "capability enables the controls")
	window.free()

	var service := FakeClient.new()
	var session = ChatSession.new()
	session.configure(service)
	var unavailable := []
	var failed := []
	var replies := []
	session.unavailable.connect(func(message: String): unavailable.append(message))
	session.failed.connect(func(message: String): failed.append(message))
	session.assistant_message.connect(func(message: String): replies.append(message))
	check(not session.is_available(), "chat starts unavailable")
	check(session.send("   ") == "", "blank text is rejected")
	check(failed.size() == 1, "blank text reports a failure")
	check(session.send("x".repeat(8001)) == "", "oversize text is rejected")
	check(failed.size() == 2, "oversize text reports a failure")
	service.features = {"chat": {"available": true, "commands": ["chat.send"]}}
	check(session.is_available(), "chat becomes available with the capability")
	var id := session.send("你好")
	check(not id.is_empty(), "valid text creates a request")
	service.reply(id, "error", {"code": "not_available", "message": "Chat is not implemented"})
	check(unavailable.size() == 1, "not_available maps to the unavailable signal")
	var invalid := session.send("你好")
	service.reply(invalid, "error", {"code": "invalid_request", "message": "bad request"})
	check(failed.size() == 3, "invalid_request maps to the failure signal")
	var accepted := session.send("你好")
	service.reply(accepted, "result", {"message": "占位回复"})
	check(replies.size() == 1 and replies[0] == "占位回复", "result maps to an assistant message")

	# The reply's animation request must reach the window. `none` and a missing
	# field both mean "say it without acting it out" and must stay silent.
	# `chat.done` is the path the service actually uses; the flat `result` frame
	# is the one-shot backend fallback and carries the action at the top level.
	var actions := []
	session.action_requested.connect(func(action: String): actions.append(action))
	var streamed := session.send("跳个舞")
	service.event(streamed, "chat.done", {"message": {"content": "好呀", "action": "dancing"}})
	check(actions == ["dancing"], "a done frame's action reaches the window, got %s" % [actions])
	var quiet := session.send("嗯")
	service.event(quiet, "chat.done", {"message": {"content": "嗯", "action": "none"}})
	check(actions.size() == 1, "an explicit none requests no action")
	var bare := session.send("嗯嗯")
	service.event(bare, "chat.done", {"message": {"content": "嗯嗯"}})
	check(actions.size() == 1, "a missing action field requests no action")
	var flat := session.send("再跳一个")
	service.reply(flat, "result", {"content": "好呀", "action": "spinning"})
	check(actions.size() == 2 and actions[1] == "spinning", "a flat result frame carries its action, got %s" % [actions])

	print("CHAT_FRAMEWORK_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

class FakeClient:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	signal event_received(id: String, type: String, payload: Dictionary)
	var state := "ready"
	var features := {}
	var personas: Array = []
	var _sequence := 0

	func request(_type: String, _payload: Dictionary, _timeout := 8.0) -> String:
		_sequence += 1
		return "fake-%d" % _sequence

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)

	func event(id: String, type: String, payload: Dictionary) -> void:
		event_received.emit(id, type, payload)
