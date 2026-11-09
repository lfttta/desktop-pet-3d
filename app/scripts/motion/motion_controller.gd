class_name PetMotionController
extends RefCounted

## Keeps the character's soles in one coordinate space: screen pixels.
##
## The pet's ground is the desktop work area's bottom edge. A transparent HWND
## that merely moves does not make the character fall, so every vertical motion
## (hold, release, fall, land) is integrated here and the window position is
## derived from `anchor` afterwards. Keeping this arithmetic out of the window
## class makes it testable without a display server.

enum State { GROUNDED, HELD, FALLING }

const DEFAULT_GRAVITY := 2600.0
const DEFAULT_MAX_FALL_SPEED := 2600.0
const DEFAULT_HOLD_FOLLOW := 18.0
const DEFAULT_HOLD_DAMPING := 7.0
const DEFAULT_MAX_HOLD_SPEED := 2400.0
const DEFAULT_AIR_DRAG := 2.2
const DEFAULT_LANDING_THRESHOLD := 220.0
const DEFAULT_MAX_INERTIA := 4.0
const REST_SPEED := 12.0

var state := State.GROUNDED
## Soles position in screen pixels; x is the subject's horizontal center.
var anchor := Vector2.ZERO
var velocity := Vector2.ZERO
var gravity := DEFAULT_GRAVITY
var max_fall_speed := DEFAULT_MAX_FALL_SPEED
var hold_follow := DEFAULT_HOLD_FOLLOW
var hold_damping := DEFAULT_HOLD_DAMPING
var max_hold_speed := DEFAULT_MAX_HOLD_SPEED
var air_drag := DEFAULT_AIR_DRAG
var landing_threshold := DEFAULT_LANDING_THRESHOLD
var max_inertia := DEFAULT_MAX_INERTIA
var work_area := Rect2i()
var subject_size := Vector2(120, 400)
## Smoothed change in velocity; the spring bones use it as an inertia input.
var acceleration := Vector2.ZERO

var _grab_offset := Vector2.ZERO
var _hold_target := Vector2.ZERO
var _landing_speed := 0.0
var _landed := false
var _started_falling := false
var _previous_velocity := Vector2.ZERO
var _last_landing_speed := 0.0
var _pending_recovery := false


func set_bounds(p_work_area: Rect2i, p_subject_size: Vector2) -> void:
	work_area = p_work_area
	subject_size = Vector2(maxf(1.0, p_subject_size.x), maxf(1.0, p_subject_size.y))
	anchor = _clamp_anchor(anchor)


## Feet cannot leave the desktop: horizontally inside the work area, never
## above its top edge, and never below its bottom edge. While held the pet may
## be lifted above the ground, so only the top and sides are clamped.
func _clamp_anchor(value: Vector2) -> Vector2:
	if work_area.size.x <= 0 or work_area.size.y <= 0:
		return value
	var half := subject_size.x * 0.5
	var minimum_x := float(work_area.position.x) + half
	var maximum_x := float(work_area.end.x) - half
	var minimum_y := float(work_area.position.y) + subject_size.y
	var maximum_y := float(work_area.end.y)
	return Vector2(
		clampf(value.x, minf(minimum_x, maximum_x), maxf(minimum_x, maximum_x)),
		clampf(value.y, minf(minimum_y, maximum_y), maxf(minimum_y, maximum_y))
	)


func ground_y() -> float:
	return float(work_area.end.y)


func is_grounded() -> bool:
	return state == State.GROUNDED


func is_held() -> bool:
	return state == State.HELD


func is_falling() -> bool:
	return state == State.FALLING


## Places the pet on the ground with its soles at the work area's bottom edge.
func settle_at(x: float) -> void:
	velocity = Vector2.ZERO
	acceleration = Vector2.ZERO
	state = State.GROUNDED
	anchor = _clamp_anchor(Vector2(x, ground_y()))
	_grab_offset = Vector2.ZERO
	_landed = false
	_started_falling = false


## Starts a hold. The grab offset keeps the body from snapping to the cursor.
## The anchor is the soles, but the character is grabbed by the body, so the
## horizontal offset is taken from the current anchor and the vertical one is
## `grab_height`: the point clicked on the body sits `grab_height` above the
## soles, so the soles must stay that far below the cursor. Subtracting the
## current anchor height here as well would cancel `grab_height` out and pull
## the soles up to the cursor the instant the mouse is pressed.
func begin_hold(cursor: Vector2, grab_height := 0.0) -> void:
	if state == State.HELD:
		return
	_grab_offset = Vector2(anchor.x - cursor.x, grab_height)
	state = State.HELD
	velocity = Vector2.ZERO
	acceleration = Vector2.ZERO
	_previous_velocity = Vector2.ZERO
	_landed = false
	_started_falling = false
	_hold_target = _clamp_anchor(cursor + _grab_offset)


## Convenience for tests and headless drivers: steer and integrate in one call.
## The window uses `steer()` plus the per-frame `update()` so one frame cannot
## advance the spring twice.
func hold(cursor: Vector2, delta: float) -> void:
	if state != State.HELD:
		return
	steer(cursor)
	update(delta)


## Sets where the held body should follow to, without integrating. The input
## loop may see several cursor samples per frame, but the body must advance at
## most once per frame, so only the target is updated here.
func steer(cursor: Vector2) -> void:
	if state != State.HELD:
		return
	_hold_target = _clamp_anchor(cursor + _grab_offset)


## Ends the hold. The pet keeps whatever horizontal momentum the drag gave it
## and starts falling from its current height.
func release() -> void:
	if state != State.HELD:
		return
	state = State.FALLING
	velocity.y = maxf(velocity.y, 0.0)
	_started_falling = true
	_landed = false


## Advances the body by one frame. Held: follows the steered target with a damped
## spring. Falling: integrates gravity and air drag. Landing is detected from the
## anchor, not from the animation, so any release height resolves correctly.
func update(delta: float) -> void:
	_landed = false
	_started_falling = false
	if delta <= 0.0:
		return
	if state == State.HELD:
		var desired := (_hold_target - anchor) * hold_follow
		velocity = velocity.lerp(desired, clampf(hold_damping * delta, 0.0, 1.0))
		# A cursor jump (or a teleporting grab offset) must not become a fling.
		velocity = velocity.limit_length(max_hold_speed)
		anchor += velocity * delta
		anchor = _clamp_anchor(anchor)
		_track_acceleration(delta)
		return
	if state != State.FALLING:
		velocity = Vector2.ZERO
		_track_acceleration(delta)
		return
	velocity.y = minf(velocity.y + gravity * delta, max_fall_speed)
	velocity.x = move_toward(velocity.x, 0.0, air_drag * absf(velocity.x) * delta)
	anchor += velocity * delta
	anchor = _clamp_anchor(anchor)
	if anchor.y >= ground_y() - 0.001:
		_landing_speed = velocity.y
		_last_landing_speed = _landing_speed
		velocity = Vector2.ZERO
		state = State.GROUNDED
		_landed = true
	_track_acceleration(delta)


func _track_acceleration(delta: float) -> void:
	if delta <= 0.0:
		return
	var raw := (velocity - _previous_velocity) / delta
	_previous_velocity = velocity
	# Smoothing keeps a single hitch from throwing the spring bones around.
	acceleration = acceleration.lerp(raw, clampf(12.0 * delta, 0.0, 1.0))


## Returns true once per landing and clears the flag. The impact speed is frozen
## here so a later reset of `_last_landing_speed` cannot change the decision.
func take_landing() -> bool:
	if not _landed:
		return false
	_landed = false
	_pending_recovery = _landing_speed >= landing_threshold
	return true


func take_fall_start() -> bool:
	if not _started_falling:
		return false
	_started_falling = false
	return true


func last_landing_speed() -> float:
	return _last_landing_speed


## True when the landing was hard enough to need the recovery cushion. Only valid
## after `take_landing()` consumed the event: asking before that would clamp the
## previous landing's speed and show a recovery for a soft drop.
func needs_recovery() -> bool:
	return _pending_recovery


## Clears the pending recovery decision so the next landing decides for itself.
func clear_landing() -> void:
	_pending_recovery = false


func is_resting() -> bool:
	return state == State.GROUNDED and velocity.length() <= REST_SPEED


## Inertia for the spring bones, expressed in character space. The pet faces the
## viewer, so screen x is a horizontal shove and screen y is vertical bounce.
## The result is clamped: an unbounded shove would fling hair and skirts apart.
func inertia() -> Vector3:
	var strength := 1.0 / 900.0
	var value := Vector3(acceleration.x * strength, -acceleration.y * strength, 0.0)
	return value.limit_length(max_inertia)
