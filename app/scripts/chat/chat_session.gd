extends RefCounted
## Chat request boundary. Transport and generation stay outside this class.

signal user_message(text: String)
signal assistant_message(text: String)
signal request_sent(id: String, text: String)
signal unavailable(message: String)
signal failed(message: String)

const MAX_TEXT_LENGTH := 8000

var _service
var _pending := {}

func configure(service) -> void:
	if _service and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	_service = service
	if _service:
		_service.request_finished.connect(_on_request_finished)

func is_available() -> bool:
	if _service == null:
		return false
	var chat: Variant = _service.features.get("chat", {})
	return chat is Dictionary and bool(chat.get("available", false))

func send(text: String) -> String:
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
	var id: String = _service.request("chat.send", {"text": trimmed})
	if id.is_empty():
		failed.emit("后台服务不可用，消息未发送。")
		return ""
	_pending[id] = trimmed
	user_message.emit(trimmed)
	request_sent.emit(id, trimmed)
	return id

func cancel() -> String:
	if _service == null:
		return ""
	return _service.request("chat.cancel", {})

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _pending.has(id):
		return
	_pending.erase(id)
	if type == "result":
		var reply := str(payload.get("message", payload.get("text", "")))
		if reply.is_empty():
			unavailable.emit("后台服务未返回聊天内容。")
		else:
			assistant_message.emit(reply)
		return
	var code := str(payload.get("code", ""))
	var message := str(payload.get("message", "聊天请求失败。"))
	if type == "error" and code == "not_available":
		unavailable.emit(message)
	else:
		failed.emit(message)
