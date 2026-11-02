class_name PetActionController
extends RefCounted

signal action_changed(action: String)

const SOURCE_PRIORITY := {
	"idle": 0, "autonomous": 10, "chat": 30, "user": 50,
	"recovery": 70, "fall": 75, "drag": 80, "hidden": 90, "exit": 100,
}

var current_action := "idle"
var current_request := 0
var current_source := "idle"
var _current_priority := 0
var _held := false
var _player: AnimationPlayer
var _actions := {}
var _prefix := "shared/"


func configure(player, actions: Dictionary, prefix := "shared/") -> void:
	_player = player as AnimationPlayer
	_actions = actions.duplicate(true)
	_prefix = str(prefix)
	reset()


func request_action(name: String, source := "user", speed := 1.0, direction := 1, hold := false) -> int:
	if not _actions.has(name):
		return -1
	var priority := int(SOURCE_PRIORITY.get(source, SOURCE_PRIORITY.user))
	if current_action != "idle" and priority < _current_priority:
		return -1
	current_request += 1
	current_action = name
	current_source = source
	_current_priority = priority
	_held = hold or str(_actions[name].get("mode", "once")) == "hold"
	if _player:
		var animation_name := _prefix + name
		if _player.has_animation(animation_name):
			var blend := float(_actions[name].get("blend_seconds", 0.15))
			_player.play(animation_name, blend, float(speed) * (1.0 if int(direction) >= 0 else -1.0))
	action_changed.emit(current_action)
	return current_request


func complete(request_id: int) -> void:
	if request_id != current_request or _held:
		return
	if str(_actions.get(current_action, {}).get("mode", "once")) == "once":
		_start_idle()


func release_action(request_id: int) -> void:
	if request_id == current_request and _held:
		_start_idle()


func reset() -> void:
	current_request += 1
	if _player:
		_player.stop()
	_start_idle(false)


func _start_idle(play := true) -> void:
	current_action = "idle"
	current_source = "idle"
	_current_priority = 0
	_held = false
	if play and _player and _player.has_animation(_prefix + "idle"):
		_player.play(_prefix + "idle", 0.12)
	action_changed.emit(current_action)
