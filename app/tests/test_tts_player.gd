extends SceneTree
## The speech player against the async contract: `tts.synthesize` answers at
## once, the WAV arrives later as `tts.ready` (or `tts.failed`), a newer line
## stops the old one and supersedes its events, and voice.enabled gates it all.

const TtsPlayer = preload("res://scripts/audio/tts_player.gd")
const MainWindow = preload("res://scripts/main.gd")

var checks := 0
var failures := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	# A script error aborts _run before its quit, which would hang the headless
	# process; this watchdog guarantees the exit either way.
	create_timer(60.0).timeout.connect(func(): quit(3))
	call_deferred("_run")

func _run() -> void:
	_test_play_stop_and_supersede()
	_test_failures_stay_silent()
	_test_voice_switch()
	_test_client_relay()
	print("TTS_PLAYER_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _test_play_stop_and_supersede() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeClient.new()
	host.add_child(fake)
	var player := TtsPlayer.new()
	host.add_child(player)
	player.configure(fake)
	var wav := _write_wav()
	check(not wav.is_empty(), "a tiny WAV file is written for the player")
	check(player.pending_count() == 0, "the player starts with nothing pending")

	# speak queues one request and waits for the async answer.
	var first := player.speak("你好")
	check(not first.is_empty(), "speak sends a request")
	check(fake.requests.size() == 1 and fake.requests[0].type == "tts.synthesize",
		"speak asks for tts.synthesize")
	check(str(fake.requests[0].payload.text) == "你好", "speak sends the trimmed line")
	check(player.pending_count() == 1, "one line waits for its answer")

	# The immediate result carries the synthesis id the events will use.
	fake.reply(first, "result", {"request_id": "tts-1", "accepted": true})
	check(player.pending_count() == 1, "the line still waits for tts.ready")

	# tts.ready loads and plays the rendered file.
	fake.event("tts-1", "tts.ready", {"id": "tts-1", "path": wav, "voice": "", "cached": false})
	var speaker := player.get_node("SpeechPlayer") as AudioStreamPlayer
	check(speaker.stream != null, "tts.ready loads the rendered WAV")
	check((speaker.stream as AudioStreamWAV).data.size() == 8,
		"the loaded stream is the rendered PCM")

	# A second line stops the first and supersedes its events.
	var second := player.speak("第二条")
	check(not second.is_empty() and second != first, "a new line sends a new request")
	check(speaker.stream == null, "the new line stops the old playback")
	fake.reply(second, "result", {"request_id": "tts-2", "accepted": true})
	fake.event("tts-1", "tts.ready", {"id": "tts-1", "path": wav, "voice": "", "cached": true})
	check(speaker.stream == null, "a superseded tts.ready is ignored")

	# The current line's failure stays silent and frees the slot.
	fake.event("tts-2", "tts.failed", {"id": "tts-2", "message": "Speech synthesis is unavailable."})
	check(speaker.stream == null and player.pending_count() == 0, "tts.failed stays silent")

	# A synchronously refused request frees the slot as well.
	var third := player.speak("再说一次")
	check(not third.is_empty(), "a third line is sent")
	fake.reply(third, "error", {"code": "voice_disabled", "message": "Speaking is turned off in settings."})
	check(player.pending_count() == 0, "an error reply frees the pending slot")

	# stop() forgets the pending line and silences the player.
	var fourth := player.speak("停不下来")
	check(player.pending_count() == 1, "the fourth line waits again")
	player.stop()
	check(player.pending_count() == 0, "stop() drops the pending line")
	check(speaker.stream == null and not speaker.playing, "stop() clears the player")

	# A blank line and a not-ready service never send.
	check(player.speak("   ") == "", "blank text sends nothing")
	fake.state = "starting"
	check(player.speak("没人") == "", "speak refuses while the service is not ready")
	check(fake.requests.size() == 4, "refusals send no request")
	host.free()

func _test_failures_stay_silent() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeClient.new()
	host.add_child(fake)
	var player := TtsPlayer.new()
	host.add_child(player)
	player.configure(fake)
	var wav := _write_wav()

	var id := player.speak("读我")
	fake.reply(id, "result", {"request_id": "tts-9", "accepted": true})
	fake.event("tts-9", "tts.ready", {"id": "tts-9", "path": wav, "voice": "", "cached": false})
	var speaker := player.get_node("SpeechPlayer") as AudioStreamPlayer
	check(speaker.stream != null, "the first ready line plays")

	# A missing path or an unparsable file degrades to silence, not to an error.
	player.speak("空路径")
	var second: String = ""
	for request in fake.requests:
		if str(request.payload.text) == "空路径":
			second = str(request.id)
	fake.reply(second, "result", {"request_id": "tts-10", "accepted": true})
	fake.event("tts-10", "tts.ready", {"id": "tts-10", "path": "", "voice": "", "cached": false})
	check(player.pending_count() == 0, "an empty path still frees the slot")
	host.free()

func _test_voice_switch() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeClient.new()
	host.add_child(fake)
	var player := TtsPlayer.new()
	host.add_child(player)
	player.configure(fake)

	player.set_voice_enabled(false)
	check(player.speak("别读") == "", "voice.enabled=false sends nothing")
	check(fake.requests.is_empty(), "the disabled switch sends no request")

	player.set_voice_enabled(true)
	var id := player.speak("再读")
	check(not id.is_empty() and fake.requests.size() == 1, "re-enabling speaks again")

	# Disabling while a line waits stops it and forgets the pending request.
	player.set_voice_enabled(false)
	check(player.pending_count() == 0, "disabling drops the pending line")
	check(player.get_node("SpeechPlayer").stream == null, "disabling stops the player")
	host.free()

## The app-side client must relay unpending tts frames, exactly like timer.due.
func _test_client_relay() -> void:
	var client := MainWindow.DesktopServiceClient.new()
	var seen: Array = []
	client.event_received.connect(func(id: String, type: String, payload: Dictionary):
		seen.append([id, type, payload]))

	client._dispatch({"version": 1, "id": "tts-1", "type": "tts.ready",
		"payload": {"id": "tts-1", "path": "line.wav"}})
	check(seen.size() == 1 and seen[0][0] == "tts-1" and seen[0][1] == "tts.ready",
		"an unpending tts.ready is relayed to event_received")
	client._dispatch({"version": 1, "id": "tts-2", "type": "tts.failed",
		"payload": {"id": "tts-2", "message": "unavailable"}})
	check(seen.size() == 2 and seen[1][1] == "tts.failed", "tts.failed is relayed too")
	client._dispatch({"version": 1, "id": "tts-3", "type": "autonomy.bubble", "payload": {}})
	check(seen.size() == 3 and seen[2][1] == "autonomy.bubble",
		"the autonomy.bubble relay is preserved")
	client._dispatch({"version": 1, "id": "tts-4", "type": "something.else", "payload": {}})
	check(seen.size() == 3, "unknown unpending types stay dropped")

	# A frame whose id is still pending is emitted once, by the stock client.
	client.pending["g1-r9"] = {"type": "settings.get", "token": "g1-r9",
		"stream_id": "", "deadline": 0, "timeout_ms": 1000}
	client._dispatch({"version": 1, "id": "g1-r9", "type": "note", "payload": {}})
	check(seen.size() == 4 and seen[3][0] == "g1-r9", "a pending frame is emitted exactly once")
	client.free()

## Writes a 44-byte canonical PCM16 mono WAV so `AudioStreamWAV.load_from_file`
## has real audio to parse without shipping a fixture.
func _write_wav() -> String:
	var pcm := PackedByteArray([0x00, 0x00, 0x34, 0x12, 0x00, 0x00, 0xcb, 0xed])
	var bytes := PackedByteArray()
	bytes.resize(44 + pcm.size())
	bytes.encode_u32(0, 0x46464952)   # "RIFF"
	bytes.encode_u32(4, 36 + pcm.size())
	bytes.encode_u32(8, 0x45564157)   # "WAVE"
	bytes.encode_u32(12, 0x20746D66)  # "fmt "
	bytes.encode_u32(16, 16)
	bytes.encode_u16(20, 1)           # PCM
	bytes.encode_u16(22, 1)           # mono
	bytes.encode_u32(24, 22050)
	bytes.encode_u32(28, 22050 * 2)
	bytes.encode_u16(32, 2)
	bytes.encode_u16(34, 16)
	bytes.encode_u32(36, 0x61746164)  # "data"
	bytes.encode_u32(40, pcm.size())
	for index in pcm.size():
		bytes[44 + index] = pcm[index]
	var directory := ProjectSettings.globalize_path(
		"res://../build/test-tts-player-%d" % OS.get_process_id())
	DirAccess.make_dir_recursive_absolute(directory)
	var path := directory.path_join("line.wav")
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return ""
	file.store_buffer(bytes)
	file.close()
	return path

class FakeClient extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	var state := "ready"
	var features := {}
	var requests: Array = []
	var _sequence := 0

	func request(type: String, payload: Dictionary, _timeout := 8.0) -> String:
		_sequence += 1
		var id := "g1-r%d" % _sequence
		requests.append({"id": id, "type": type, "payload": payload.duplicate(true)})
		return id

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)

	func event(id: String, type: String, payload: Dictionary) -> void:
		event_received.emit(id, type, payload)
