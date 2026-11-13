extends RefCounted
## Line-delimited JSON frames. Anything that is not a valid v1 protocol frame
## (stray prints, tracebacks, log lines on the service's stdout) is skipped with
## a debug log instead of tearing down the transport; only a runaway buffer is
## a real transport failure.

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
		var message: Variant = _parse_line(line)
		if message == null:
			continue
		messages.append(message)
	return {"messages": messages, "error": ""}

## Returns the frame, or null when the line is not part of the protocol. Valid
## frames are parsed exactly as before; only the rejection path changed.
func _parse_line(line: PackedByteArray) -> Variant:
	var json := JSON.new()
	if json.parse(line.get_string_from_utf8()) != OK:
		_debug_skip(line, "无法解析")
		return null
	var message: Variant = json.data
	if not message is Dictionary or not message.get("id") is String or not message.get("type") is String or not message.get("payload") is Dictionary:
		_debug_skip(line, "结构不兼容")
		return null
	if typeof(message.get("version")) not in [TYPE_INT, TYPE_FLOAT] or message.version != 1:
		_debug_skip(line, "协议版本不兼容")
		return null
	return message

func _debug_skip(line: PackedByteArray, reason: String) -> void:
	var text := line.get_string_from_utf8()
	if text.length() > 120:
		text = text.substr(0, 120) + "…"
	print_verbose("[service] 跳过非协议行（", reason, "）：", text)

func _failure(detail: String) -> Dictionary:
	_buffer.clear()
	return {"messages": [], "error": detail}
