extends RefCounted
## Streaming chat state machine. Transport and generation stay outside this class.

signal user_message(text: String)
signal assistant_message(text: String)
signal delta_received(text: String)
signal request_sent(id: String, text: String)
signal accepted(id: String, request_id: String)
signal completed(message: String, usage: Dictionary, memory_ops: Array, state_delta: Dictionary)
signal error_received(code: String, message: String, retryable: bool)
signal cancelled()
signal unavailable(message: String)
signal failed(message: String)
signal history_loaded(messages: Array)
signal history_unavailable(message: String)
signal bubble_received(text: String)
## The reply asked the pet to act something out. Empty or "none" means it did not.
signal action_requested(action: String)
## The reply asked for a short line to be spoken aloud. Empty means silence.
signal speech_requested(text: String)

const MAX_TEXT_LENGTH := 8000
const CHAT_TIMEOUT := 40.0
const HISTORY_LIMIT := 50

var _service
var _pending := {}
var _history := {}
var _conversation_id := ""

func configure(service) -> void:
	_disconnect_service()
	_service = service
	if _service == null:
		return
	if _service.has_signal("request_finished") and not _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.connect(_on_request_finished)
	if _service.has_signal("event_received") and not _service.event_received.is_connected(_on_event):
		_service.event_received.connect(_on_event)

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	if _service.has_signal("event_received") and _service.event_received.is_connected(_on_event):
		_service.event_received.disconnect(_on_event)

func is_available() -> bool:
	if _service == null:
		return false
	var chat: Variant = _service.features.get("chat", {})
	return chat is Dictionary and bool(chat.get("available", false))

func is_streaming() -> bool:
	return not _pending.is_empty()

func active_request_id() -> String:
	for id: String in _pending.keys():
		var state: Dictionary = _pending[id]
		var stream_id := str(state.get("stream_id", ""))
		return stream_id if not stream_id.is_empty() else id
	return ""

func conversation_id() -> String:
	return _conversation_id

func set_conversation_id(value: String) -> void:
	_conversation_id = value.strip_edges()

func send(text: String, conversation_id := "") -> String:
	if _service == null:
		failed.emit("后台服务尚未连接。")
		return ""
	var trimmed := text.strip_edges()
	if trimmed.is_empty():
		failed.emit("消息不能为空。")
		return ""
	if trimmed.length() > MAX_TEXT_LENGTH:
		failed.emit("消息超过 8000 字。")
		return ""
	if is_streaming():
		failed.emit("上一条回复尚未结束。")
		return ""
	var conversation := conversation_id.strip_edges()
	if conversation.is_empty():
		conversation = _conversation_id
	var payload := {"text": trimmed}
	if not conversation.is_empty():
		payload["conversation_id"] = conversation
	var id: String = _service.request("chat.send", payload, CHAT_TIMEOUT)
	if id.is_empty():
		failed.emit("后台服务不可用，消息未发送。")
		return ""
	_pending[id] = {
		"text": trimmed,
		"buffer": "",
		"token": id,
		"stream_id": "",
		"cancelled": false,
	}
	user_message.emit(trimmed)
	request_sent.emit(id, trimmed)
	return id

func cancel() -> String:
	var id := active_request_id()
	if id.is_empty() or _service == null:
		return ""
	var state: Dictionary = _pending[id]
	state["cancelled"] = true
	var target := str(state.get("stream_id", ""))
	if target.is_empty():
		target = id
	var cancel_id := ""
	if _service.has_method("cancel"):
		cancel_id = _service.cancel(target)
	else:
		cancel_id = _service.request("chat.cancel", {"request_id": target}, 8.0)
	_remove_pending(id)
	cancelled.emit()
	return cancel_id

func load_history(limit := HISTORY_LIMIT) -> String:
	if _service == null:
		history_unavailable.emit("后台服务尚未连接。")
		return ""
	var payload := {"limit": limit}
	if not _conversation_id.is_empty():
		payload["conversation_id"] = _conversation_id
	var id: String = _service.request("chat.history", payload, 10.0)
	if id.is_empty():
		history_unavailable.emit("历史记录加载失败。")
		return ""
	_history[id] = true
	return id

func _on_event(id: String, type: String, payload: Dictionary) -> void:
	if type == "autonomy.bubble":
		var bubble := str(payload.get("text", ""))
		if not bubble.is_empty():
			bubble_received.emit(bubble)
		return
	if not _pending.has(id):
		return
	var state: Dictionary = _pending[id]
	if bool(state.get("cancelled", false)):
		return
	if type == "accepted":
		var conversation := str(payload.get("conversation_id", ""))
		if not conversation.is_empty():
			_conversation_id = conversation
		var stream_id := str(payload.get("request_id", ""))
		if not stream_id.is_empty() and stream_id != id:
			_bind_stream(id, stream_id)
			accepted.emit(id, stream_id)
		return
	if type == "chat.delta":
		var chunk := str(payload.get("text", ""))
		if chunk.is_empty():
			return
		state["buffer"] = str(state.get("buffer", "")) + chunk
		delta_received.emit(chunk)
		return
	if type == "chat.done":
		_finish(id, payload)
		return
	if type == "chat.error":
		_fail(id, payload)

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if _history.has(id):
		_history.erase(id)
		if type == "result":
			var conversation := str(payload.get("conversation_id", ""))
			if not conversation.is_empty():
				_conversation_id = conversation
			history_loaded.emit(_normalize_history(payload))
		elif type == "error" and str(payload.get("code", "")) == "not_available":
			history_unavailable.emit(str(payload.get("message", "历史记录未接入。")))
		else:
			history_unavailable.emit(str(payload.get("message", "历史记录加载失败。")))
		return
	if not _pending.has(id):
		return
	var state: Dictionary = _pending[id]
	if bool(state.get("cancelled", false)):
		return
	if type == "result":
		if payload.has("request_id") and not payload.has("message"):
			var stream_id := str(payload.get("request_id", ""))
			if not stream_id.is_empty() and stream_id != id:
				_bind_stream(id, stream_id)
				accepted.emit(id, stream_id)
			return
		# One-shot backends answer with a single final result frame. Its action
		# sits at the top level rather than under `message`.
		var reply := _message_text(payload)
		_remove_pending(id)
		if reply.is_empty():
			_emit_error("empty_response", "后台服务未返回聊天内容。", true)
			return
		assistant_message.emit(reply)
		_emit_action(_message_action(payload))
		_emit_speech(_message_speak(payload))
		completed.emit(reply, {}, [], {})
		return
	_fail(id, payload)

func _finish(id: String, payload: Dictionary) -> void:
	var state: Dictionary = _pending[id]
	var buffer := str(state.get("buffer", ""))
	var message_value: Variant = payload.get("message", "")
	var text := _message_text(message_value)
	if text.is_empty():
		text = buffer
	var usage_value: Variant = payload.get("usage", {})
	var memory_value: Variant = payload.get("memory_ops", [])
	var state_value: Variant = payload.get("state_delta", {})
	var usage: Dictionary = usage_value if usage_value is Dictionary else {}
	var memory_ops: Array = memory_value if memory_value is Array else []
	var state_delta: Dictionary = state_value if state_value is Dictionary else {}
	var conversation := str(payload.get("conversation_id", ""))
	if not conversation.is_empty():
		_conversation_id = conversation
	_remove_pending(id)
	if text.is_empty():
		_emit_error("empty_response", "后台服务未返回聊天内容。", true)
		return
	assistant_message.emit(text)
	# Emit before `completed` so a listener that reacts to both sees the body
	# start moving while the window still reports "replying".
	_emit_action(_message_action(message_value))
	_emit_speech(_message_speak(message_value))
	completed.emit(text, usage, memory_ops, state_delta)

func _fail(id: String, payload: Dictionary) -> void:
	var code := str(payload.get("code", ""))
	var message := str(payload.get("message", "聊天请求失败。"))
	var retryable := bool(payload.get("retryable", false))
	if code in ["timeout", "transport_error", "rate_limited", "server_error", "overloaded"]:
		retryable = true
	_remove_pending(id)
	if code == "not_available":
		unavailable.emit(message)
		return
	failed.emit(message)
	error_received.emit(code, message, retryable)

func _emit_error(code: String, message: String, retryable: bool) -> void:
	failed.emit(message)
	error_received.emit(code, message, retryable)

func _bind_stream(id: String, stream_id: String) -> void:
	if not _pending.has(id):
		return
	var state: Dictionary = _pending[id]
	state["stream_id"] = stream_id
	_pending[stream_id] = state

func _remove_pending(id: String) -> void:
	if not _pending.has(id):
		return
	var token := str(_pending[id].get("token", id))
	for key: String in _pending.keys():
		if str(_pending[key].get("token", "")) == token:
			_pending.erase(key)

func _message_text(value: Variant) -> String:
	if value is String:
		return str(value)
	if value is Dictionary:
		for key in ["content", "text", "message"]:
			if value.get(key) is String:
				return str(value[key])
	return ""

## The animation a reply asked for, or "" when it asked for none.
##
## The service validates the id against the library before sending it, but the
## app still decides: an id this build does not have is dropped by the window.
func _message_action(value: Variant) -> String:
	if not (value is Dictionary):
		return ""
	var action := str((value as Dictionary).get("action", "")).strip_edges()
	if action == "none":
		return ""
	return action

func _emit_action(action: String) -> void:
	if not action.is_empty():
		action_requested.emit(action)

## The short line a reply asked to be spoken, or "" when it asked for silence.
##
## `speak` is separate from the displayed body: an empty value must never make
## the app read the whole reply aloud.
func _message_speak(value: Variant) -> String:
	if not (value is Dictionary):
		return ""
	var data := value as Dictionary
	var speak := str(data.get("speak", "")).strip_edges()
	if not speak.is_empty():
		return speak
	# A one-shot result carries the same fields either flat or under `message`.
	var message: Variant = data.get("message")
	if message is Dictionary:
		return str((message as Dictionary).get("speak", "")).strip_edges()
	return ""

func _emit_speech(speak: String) -> void:
	if not speak.is_empty():
		speech_requested.emit(speak)

func _normalize_history(payload: Dictionary) -> Array:
	var items: Variant = payload.get("messages", [])
	if not items is Array:
		items = payload.get("items", [])
	if not items is Array:
		return []
	var result: Array = []
	for item in items:
		if item is Dictionary:
			var text := _message_text(item)
			if text.is_empty():
				continue
			result.append({"role": str(item.get("role", "")), "text": text})
		elif item is String and not item.is_empty():
			result.append({"role": "", "text": item})
	return result
