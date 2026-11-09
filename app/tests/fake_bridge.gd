extends RefCounted
## Stands in for PetWindowBridge so the input loop can be driven headlessly.
## Only the members `_update_input` touches are implemented.

var cursor := Vector2i.ZERO
var down := false
var captured := false
var interactive := false
var capture_requests := 0
var release_requests := 0


func get_cursor_position() -> Vector2i:
	return cursor


func is_left_button_down() -> bool:
	return down


func has_capture() -> bool:
	return captured


func begin_capture() -> bool:
	capture_requests += 1
	captured = true
	return true


func end_capture() -> void:
	release_requests += 1
	captured = false


func set_interactive(value: bool) -> void:
	interactive = value


func poll_tray_action() -> int:
	return 0


func shutdown() -> void:
	pass
