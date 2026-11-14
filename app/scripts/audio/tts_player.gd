extends Node
## Plays the short spoken line a reply or a local greeting asked for.
##
## The service synthesizes off its main loop now: `tts.synthesize` answers at
## once with a synthesis request id and the WAV arrives later as a `tts.ready`
## event (or `tts.failed` when the backend cannot speak). This node tracks one
## line at a time — a newer line stops the old one and supersedes its pending
## events — and stays silent on every failure path; the chat text is unaffected.

## The request round trip itself. Renders are asynchronous service-side, so no
## timeout here has to guard a slow render.
const SYNTHESIZE_TIMEOUT := 40.0

var _service
var _player: AudioStreamPlayer
var _enabled := true
## Transport request id -> synthesis request id, filled in when the immediate
## result arrives and matched again by the later `tts.ready`/`tts.failed`.
var _pending := {}

func _ready() -> void:
	_ensure_player()

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

## Applies the service's voice.enabled switch. Disabling also stops the current
## line and forgets pending ones, so the switch silences the pet immediately.
func set_voice_enabled(value: bool) -> void:
	_enabled = value
	if not _enabled:
		stop()

## Request speech for `text`. Returns the request id, or "" when nothing was sent.
func speak(text: String) -> String:
	var line := text.strip_edges()
	if line.is_empty() or not _enabled or _service == null or _service.state != "ready":
		return ""
	# A new line interrupts the old one at once; its late events are ignored
	# because only the newest request id is kept.
	stop()
	var id: String = _service.request("tts.synthesize", {"text": line}, SYNTHESIZE_TIMEOUT)
	if not id.is_empty():
		_pending[id] = ""
	return id

## Stops the current line and forgets every pending one.
func stop() -> void:
	_pending.clear()
	_ensure_player().stop()
	_player.stream = null

## How many lines wait for a synthesis answer right now.
func pending_count() -> int:
	return _pending.size()

func _ensure_player() -> AudioStreamPlayer:
	if _player == null:
		_player = AudioStreamPlayer.new()
		_player.name = "SpeechPlayer"
		add_child(_player)
	return _player

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _pending.has(id):
		return
	if type != "result":
		# `voice_disabled`, `tts_unavailable`, `tts_busy` and every other error
		# are intentionally silent: the pet stays quiet instead of surfacing a
		# failure in chat.
		_pending.erase(id)
		return
	# The service accepted the line; remember the id its events will carry.
	_pending[id] = str(payload.get("request_id", ""))

func _on_event(_id: String, type: String, payload: Dictionary) -> void:
	if type != "tts.ready" and type != "tts.failed":
		return
	var synth_id := str(payload.get("id", ""))
	if synth_id.is_empty():
		return
	var owner := ""
	for request_id: String in _pending.keys():
		if str(_pending[request_id]) == synth_id:
			owner = request_id
			break
	if owner.is_empty():
		return
	_pending.erase(owner)
	if type == "tts.failed":
		# A backend that cannot synthesize degrades to silence.
		return
	var path := str(payload.get("path", ""))
	if path.is_empty():
		return
	_play(path)

func _play(path: String) -> void:
	if not FileAccess.file_exists(path):
		return
	var stream: AudioStreamWAV = AudioStreamWAV.load_from_file(path)
	if stream == null:
		return
	var player := _ensure_player()
	player.stream = stream
	player.play()

func _exit_tree() -> void:
	_disconnect_service()
	if _player != null:
		_player.stop()
