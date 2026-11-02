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
	check(not invalid.error.is_empty(), "invalid JSON is rejected")
	decoder = script.new()
	var oversized := PackedByteArray()
	oversized.resize(1048577)
	oversized.fill(65)
	check(not decoder.feed(oversized).error.is_empty(), "unterminated oversized message has a hard bound")
	decoder = script.new()
	check(not decoder.feed('{"version":2,"id":"x","type":"ready","payload":{}}\n'.to_utf8_buffer()).error.is_empty(), "wrong protocol rejected before dispatch")
	check(not decoder.feed('[]\n'.to_utf8_buffer()).error.is_empty(), "non-object message rejected")
	print("PROTOCOL_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
