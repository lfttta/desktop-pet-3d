extends SceneTree

const MainWindow = preload("res://scripts/main.gd")
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
	# Text from the desktop bubble goes through the same path as the input box.
	check(not window.submit_text("还没接入"), "submit_text refuses while chat is unavailable")
	check(window.input.text == "还没接入", "a refused line stays in the field")
	check(fake.requests.is_empty(), "a refused line sends nothing")
	fake.features = {"chat": {"available": true, "commands": ["chat.send"]}}
	fake.capabilities_received.emit(fake.features, fake.personas)
	check(window.input.editable and not window.send_button.disabled, "capability enables the controls")
	check(not window.submit_text("   "), "blank submit_text is refused")
	check(window.submit_text("  你好  "), "submit_text sends when chat is available")
	check(fake.requests.size() == 1 and fake.requests[0]["type"] == "chat.send",
		"submit_text sends chat.send, got %s" % [fake.requests])
	check(fake.requests[0]["payload"].get("text") == "你好",
		"submit_text trims the bubble's line, got %s" % [fake.requests[0]["payload"]])
	# The first send is still streaming; a second bubble line must not vanish.
	var sent_before := fake.requests.size()
	check(not window.submit_text("流式中"), "submit_text refuses while a reply streams")
	check(fake.requests.size() == sent_before, "a refused streaming line sends nothing")
	check(window.input.text == "流式中", "the streaming refusal keeps the text in the field")
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

	# `speak` is what gets read aloud. It is independent of `action`: a reply may
	# act without speaking, speak without acting, or both. An empty field must not
	# fall back to the whole reply text, or every long answer would be read out.
	var speeches := []
	session.speech_requested.connect(func(line: String): speeches.append(line))
	var spoken := session.send("说句话")
	service.event(spoken, "chat.done", {"message": {"content": "这是一段很长的回复正文。", "speak": "你好呀"}})
	check(speeches == ["你好呀"], "a done frame's speak line reaches the player, got %s" % [speeches])
	var silent := session.send("别说")
	service.event(silent, "chat.done", {"message": {"content": "嗯", "speak": ""}})
	check(speeches.size() == 1, "an empty speak field reads nothing, got %s" % [speeches])
	var no_field := session.send("也别念")
	service.event(no_field, "chat.done", {"message": {"content": "嗯嗯"}})
	check(speeches.size() == 1, "a missing speak field reads nothing")

	# Memory candidates used to be counted and dropped, so a reply said "记住了"
	# while nothing was stored. The window now filters the pending ones so it can
	# offer 记住 / 忽略.
	var pending := ChatWindow.pending_memory_entries([
		{"operation": "propose", "status": "pending", "content": "  我喜欢猫  "},
		{"operation": "propose", "status": "stored", "content": "已写入"},
		{"operation": "propose", "status": "pending", "content": "   "},
		{"operation": "delete", "status": "pending", "content": "x"},
		"not a dict",
	])
	check(pending.size() == 1, "only pending proposals are offered, got %s" % [pending])
	check(str(pending[0].get("text", "")) == "我喜欢猫",
		"a pending proposal is trimmed, got %s" % [pending])
	check(str(pending[0].get("category", "")) == "general",
		"a candidate without a category saves as general")
	check(is_equal_approx(float(pending[0].get("importance", 0.0)), 0.5),
		"a candidate without importance gets the manual default")

	await _main_phrase_wiring()

	print("CHAT_FRAMEWORK_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)


## main.gd wires the chat session to the pet's apology and waiting pools, so
## neither library stays unreachable: a request that really failed lets the pet
## apologize once, and one still streaming past the waiting threshold earns a
## single waiting line. Every terminal signal settles the measurement.
##
## The probe subclass records the phrase calls: the real say_apology would need
## a window in the tree, and main.gd must never enter the tree in tests (its
## _ready starts the service).
func _main_phrase_wiring() -> void:
	var fake := FakeClient.new()
	fake.personas = [{"id": "godette", "display_name": "Godette"}]
	var chat: Window = ChatWindow.new()
	chat.visible = false
	root.add_child(chat)
	await process_frame
	fake.features = {"chat": {"available": true, "commands": ["chat.send"]}}
	chat.configure_chat(fake, {"persona_id": "godette"})

	var main := PhraseProbe.new()
	# The waiting measurement reads the injected clock, so the threshold is
	# crossed by moving it instead of waiting the seconds in real time.
	var clock := FakeClock.new()
	main.chat_request_clock = clock.now
	main.chat_window = chat
	main._connect_chat_phrases()
	check(main._chat_request_ms < 0, "no request is measured before one is sent")
	var session = chat.get("_session")
	check(session != null, "configure_chat created the session the wiring reads")

	# A real failure (the request went out, an error frame came back) apologizes.
	clock.ms = 1000
	var id: String = session.send("你好")
	check(main._chat_request_ms == 1000, "request_sent starts the waiting measurement")
	check(main.waitings.is_empty(), "the send itself says no waiting line")
	fake.reply(id, "error", {"code": "timeout", "message": "超时", "retryable": true})
	check(main.apologies == 1, "a failed request apologizes, got %d" % main.apologies)
	check(main._chat_request_ms < 0, "the failure settles the measurement")

	# A local input check emits `failed` alone; nothing was answered, so the pet
	# must not apologize for it.
	session.send("   ")
	check(main.apologies == 1, "a blank line sends no apology")
	check(main._chat_request_ms < 0, "a refused line leaves the measurement alone")

	# The waiting line: a young request stays silent, an aged one speaks once.
	clock.ms = 2000
	id = session.send("讲个故事")
	main._announce_chat_waiting()
	check(main.waitings.is_empty(), "a young request stays silent")
	clock.ms = 2000 + int(MainWindow.WAITING_PHRASE_SECONDS * 1000.0) - 1
	main._announce_chat_waiting()
	check(main.waitings.is_empty(), "7999 ms is still inside the threshold")
	clock.ms = 2000 + int(MainWindow.WAITING_PHRASE_SECONDS * 1000.0)
	main._announce_chat_waiting()
	check(main.waitings.size() == 1, "a request past the threshold says a waiting line, got %s" % [main.waitings])
	check(is_equal_approx(float(main.waitings[0]), MainWindow.WAITING_PHRASE_SECONDS),
		"the waiting line carries the elapsed seconds, got %s" % [main.waitings[0]])
	main._announce_chat_waiting()
	check(main.waitings.size() == 1, "one waiting attempt per request")

	# The reply settles the request, and a settled one announces nothing again.
	fake.reply(id, "result", {"message": "讲完了"})
	check(main._chat_request_ms < 0, "the reply settles the measurement")
	main._announce_chat_waiting()
	check(main.waitings.size() == 1, "a settled request announces nothing")

	# Cancelling settles too.
	id = session.send("再来一个")
	check(main._chat_request_ms >= 0, "a second send restarts the measurement")
	session.cancel()
	check(main._chat_request_ms < 0, "cancelling settles the measurement")
	main._announce_chat_waiting()
	check(main.waitings.size() == 1, "a cancelled request announces nothing")

	# `not_available` settles through `unavailable`; it is a missing capability,
	# not an answer that went wrong, so it stays apology-free.
	id = session.send("在吗")
	fake.reply(id, "error", {"code": "not_available", "message": "Chat is not implemented"})
	check(main.apologies == 1, "a not_available error does not apologize")
	check(main._chat_request_ms < 0, "unavailable settles the measurement")

	chat.free()
	main.free()

class FakeClient:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	signal event_received(id: String, type: String, payload: Dictionary)
	var state := "ready"
	var features := {}
	var personas: Array = []
	var requests: Array = []
	var _sequence := 0

	func request(type: String, payload: Dictionary, _timeout := 8.0) -> String:
		_sequence += 1
		requests.append({"type": type, "payload": payload.duplicate(true)})
		return "fake-%d" % _sequence

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)

	func event(id: String, type: String, payload: Dictionary) -> void:
		event_received.emit(id, type, payload)


## main.gd must never enter the tree in tests (its _ready starts the service),
## so the wiring test records the phrase calls through this subclass instead of
## showing real bubbles.
class PhraseProbe extends MainWindow:
	var apologies := 0
	var waitings: Array = []

	func say_apology() -> bool:
		apologies += 1
		return true

	func say_waiting(elapsed_seconds: float) -> bool:
		waitings.append(elapsed_seconds)
		return true


## Tick source for `chat_request_clock`, so the test moves past the waiting
## threshold in one assignment.
class FakeClock extends RefCounted:
	var ms := 0

	func now() -> int:
		return ms
