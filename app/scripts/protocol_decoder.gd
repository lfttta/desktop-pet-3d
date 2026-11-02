extends RefCounted

const LIMIT := 1048576
var _buffer := PackedByteArray()

func feed(chunk: PackedByteArray) -> Dictionary:
	_buffer.append_array(chunk)
	var messages: Array = []
	while true:
		var newline := _buffer.find(10)
		if newline < 0:
			if _buffer.size() > LIMIT:
				return _failure("后台服务消息超过 1 MiB")
			break
		if newline > LIMIT:
			return _failure("后台服务消息超过 1 MiB")
		var line := _buffer.slice(0, newline)
		_buffer = _buffer.slice(newline + 1)
		if line.is_empty():
			continue
		var json := JSON.new()
		if json.parse(line.get_string_from_utf8()) != OK:
			return _failure("后台服务返回了无法解析的消息")
		var message: Variant = json.data
		if not message is Dictionary or not message.get("id") is String or not message.get("type") is String or not message.get("payload") is Dictionary:
			return _failure("后台服务消息结构不兼容")
		if typeof(message.get("version")) not in [TYPE_INT, TYPE_FLOAT] or message.version != 1:
			return _failure("后台服务协议版本不兼容")
		messages.append(message)
	return {"messages": messages, "error": ""}

func _failure(detail: String) -> Dictionary:
	_buffer.clear()
	return {"messages": [], "error": detail}
