extends SceneTree
var checks := 0
var failures := 0
func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)
func _initialize() -> void:
	var path := "res://scripts/protocol_decoder.gd"
	if not ResourceLoader.exists(path):
		print("PROTOCOL_TESTS missing decoder")
		quit(1)
		return
	var script: Script = load(path)
	var decoder: RefCounted = script.new()
	var bytes := '{"version":1,"id":"a","type":"result","payload":{"name":"中文"}}\n'.to_utf8_buffer()
	var count := 0
	for value in bytes:
		var result: Dictionary = decoder.feed(PackedByteArray([value]))
		for message: Dictionary in result.messages:
			count += 1
			check(message.payload.name == "中文", "split UTF8 stays intact")
	check(count == 1, "one byte at a time emits one message")
	var pair: Dictionary = decoder.feed(bytes + bytes)
	check(pair.messages.size() == 2, "coalesced messages remain separate")
	var invalid: Dictionary = decoder.feed('{bad}\n'.to_utf8_buffer())
	check(invalid.error.is_empty() and invalid.messages.is_empty(), "invalid JSON is skipped, not fatal")
	var after_garbage: Dictionary = decoder.feed(bytes)
	check(after_garbage.error.is_empty() and after_garbage.messages.size() == 1, "a valid frame after garbage still parses")
	decoder = script.new()
	var oversized := PackedByteArray()
	oversized.resize(1048577)
	oversized.fill(65)
	check(not decoder.feed(oversized).error.is_empty(), "unterminated oversized message has a hard bound")
	decoder = script.new()
	check(decoder.feed('{"version":2,"id":"x","type":"ready","payload":{}}\n'.to_utf8_buffer()).messages.is_empty(), "wrong protocol version is skipped")
	check(decoder.feed('[]\n'.to_utf8_buffer()).messages.is_empty(), "non-object message is skipped")
	# A service that prints logs/tracebacks to stdout must not kill the transport:
	# garbage interleaved between real frames is dropped, frames survive intact.
	decoder = script.new()
	var frame_a := '{"version":1,"id":"a","type":"result","payload":{"n":1}}\n'
	var frame_b := '{"version":1,"id":"b","type":"event","payload":{"n":2}}\n'
	var mixed := ("starting up...\n" + frame_a + "Traceback (most recent call last):\n" + '  File "x.py", line 1\n' + "[1, 2, 3]\n" + '{"version":2,"id":"z","type":"ready","payload":{}}\n' + frame_b + "\n").to_utf8_buffer()
	var interleaved: Dictionary = decoder.feed(mixed)
	check(interleaved.error.is_empty(), "interleaved garbage is not a transport failure")
	check(interleaved.messages.size() == 2, "both real frames survive the garbage")
	check(interleaved.messages[0].id == "a" and interleaved.messages[1].id == "b", "frame order and ids are preserved")
	# Garbage split across chunk boundaries is skipped byte by byte too.
	decoder = script.new()
	var split_count := 0
	var split_error := false
	for value in "plain log line\n".to_utf8_buffer():
		var piece: Dictionary = decoder.feed(PackedByteArray([value]))
		split_error = split_error or not str(piece.error).is_empty()
		split_count += piece.messages.size()
	check(not split_error and split_count == 0, "a split garbage line never errors or emits")
	var tail: Dictionary = decoder.feed(bytes)
	check(tail.messages.size() == 1, "the frame after split garbage still parses")
	print("PROTOCOL_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
