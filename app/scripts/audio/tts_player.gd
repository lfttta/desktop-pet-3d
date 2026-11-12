extends Node
## Plays the short spoken line a reply asked for.
##
## The service renders the line to a WAV file; this node only requests the file
## and plays it. One line plays at a time — a newer line stops the old one and
## supersedes its pending request. A backend that cannot synthesize degrades to
## silence; the chat text is unaffected.

## The service renders one line in at most 20 s, but a newer request may queue
## behind an older one; this stays well above one render so a busy voice never
## looks like a dead transport.
const SYNTHESIZE_TIMEOUT := 40.0

var _service
var _player: AudioStreamPlayer
var _request_id := ""

func _ready() -> void:
	_ensure_player()

func configure(service) -> void:
	_disconnect_service()
	_service = service
	if _service == null:
		return
	if _service.has_signal("request_finished") and not _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.connect(_on_request_finished)

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)

## Request speech for `text`. Returns the request id, or "" when nothing was sent.
func speak(text: String) -> String:
	var line := text.strip_edges()
	if line.is_empty() or _service == null or _service.state != "ready":
		return ""
	# A new line interrupts the old one at once; its late response is ignored
	# because only the newest request id is kept.
	_request_id = ""
	_ensure_player().stop()
	var id: String = _service.request("tts.synthesize", {"text": line}, SYNTHESIZE_TIMEOUT)
	if not id.is_empty():
		_request_id = id
	return id

func _ensure_player() -> AudioStreamPlayer:
	if _player == null:
		_player = AudioStreamPlayer.new()
		_player.name = "SpeechPlayer"
		add_child(_player)
	return _player

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if id != _request_id:
		return
	_request_id = ""
	if type != "result":
		# `tts_unavailable` (and any other backend error) is intentionally silent:
		# the pet simply stays quiet instead of surfacing a failure in chat.
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
