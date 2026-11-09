extends RefCounted
## Pure drag state. Platform capture is acquired/released by the caller.

## Movement needed before a press counts as a drag. Everything here is in screen
## physical pixels, and at 150% display scaling Windows' own drag threshold of
## 4 DIPs is 6 physical pixels; 8 keeps a shaky click from lifting the pet.
const DRAG_THRESHOLD := 8.0

var active := false
var dragging := false
var _previous_down := false
var _press := Vector2i.ZERO
var _offset := Vector2i.ZERO

func update(down: bool, hit: bool, cursor: Vector2i, window_position: Vector2i, captured: bool, enabled: bool) -> Dictionary:
	var result := {"begin": false, "end": false, "click": false, "moved": false, "position": window_position}
	var rising := down and not _previous_down
	_previous_down = down
	if active:
		if Vector2(cursor - _press).length() >= DRAG_THRESHOLD:
			dragging = true
		if not enabled or not captured or not down:
			result.end = true
			# Godot/Win32 may release capture while dispatching mouse-up, before
			# this polling tick. Only a loss while still held is cancellation.
			result.click = enabled and not down and not dragging
			if enabled and not down and dragging:
				result.moved = true
				result.position = cursor - _offset
			active = false
			dragging = false
			return result
		if dragging:
			result.moved = true
			result.position = cursor - _offset
	elif enabled and rising and hit:
		active = true
		dragging = false
		_press = cursor
		_offset = cursor - window_position
		result.begin = true
	return result

func cancel() -> void:
	active = false
	dragging = false
