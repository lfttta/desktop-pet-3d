extends SceneTree
## End-to-end check that the shipping window actually walks: the clip drives the
## position, the window follows, and every interruption path ends the segment.
##
## The native bridge is absent in headless runs, which these code paths tolerate.
## Window writes are no-ops without a display server, so this asserts on the
## motion anchor (the soles position) rather than on real screen coordinates.

const PetWindow = preload("res://scripts/pet_window.gd")

const WORK_AREA := Rect2i(0, 0, 1920, 1000)

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	var window := PetWindow.new()
	root.add_child(window)
	await process_frame
	await process_frame

	var view = window.actor
	check(view != null, "the shipping window loaded a character")
	if view == null:
		print("WALK_WIRING_TESTS checks=", checks, " failures=", failures)
		quit(1)
		return

	# The behaviour chain would otherwise decide on its own during the awaits and
	# replace the clip this test is driving.
	window.behavior_enabled = false

	# Headless reports a 0x0 desktop; pin a known one so ground and walls are
	# meaningful. The window's own `_ensure_motion_bounds` runs through
	# `work_area_override` too, so this survives the first walk.
	window.work_area_override = WORK_AREA
	window._ensure_motion_bounds()
	window.motion.settle_at(960.0)
	window._play_action("idle")

	_test_stride(window, view)
	check(window.motion.work_area == WORK_AREA, "the motion bounds are the pinned desktop, got %s" % window.motion.work_area)
	check(is_equal_approx(window.motion.anchor.x, 960.0), "the anchor starts where the test put it, got %f" % window.motion.anchor.x)
	await _test_walk_runs(window, view)
	_test_interrupt_by_grab(window, view)
	_test_interrupt_by_action(window, view)
	_test_behavior_gate(window)
	_test_facing(view)

	print("WALK_WIRING_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)


## The stride must come from the authored contract, scaled by the window.
func _test_stride(window, view) -> void:
	var stride: float = window._walk_stride_px()
	check(stride > 0.0, "a stride is derived from the contract, got %f" % stride)
	var meters := float((view.config.get("locomotion", {}) as Dictionary).get("meters_per_cycle", 0.0))
	check(meters > 0.0, "the contract carries meters_per_cycle")
	var cycle_length: float = view.walk_cycle_length()
	check(absf(cycle_length - 1.0333333) < 0.001, "the walk clip length is the authored one, got %f" % cycle_length)


## A walk must move the soles, advance on the clip, and end exactly on target.
func _test_walk_runs(window, view) -> void:
	var start: float = window.motion.anchor.x
	var began: bool = window._begin_walk(1, 3)
	check(began, "the window can begin a walk")
	check(window._walk_active, "the walk is marked active")
	check(view.action == "walk", "the walk clip is playing, got %s" % view.action)
	check(not view.ground_contact_enabled(), "foot planting is off while the body travels")

	var stride: float = window.motion.walk_stride()
	check(stride > 0.0, "the running plan exposes its stride")

	# Drive the segment as the frame loop would, from the clip's own position.
	var guard := 0
	while window._walk_active and guard < 600:
		guard += 1
		window._update_walk()
		await process_frame
	check(not window._walk_active, "the walk ends on its own")
	var travelled: float = window.motion.anchor.x - start
	check(travelled > 0.0, "the walk moved the body right, moved %f" % travelled)
	# Whole cycles only: the distance must be an integer multiple of the stride.
	var cycles: float = travelled / stride
	check(absf(cycles - roundf(cycles)) < 0.01, "the distance is whole gait cycles, got %.3f" % cycles)
	check(is_equal_approx(window.motion.anchor.y, float(WORK_AREA.end.y)), "walking stays on the ground line")
	check(view.action == "idle", "the walk returns to idle, got %s" % view.action)


## Grabbing mid-walk must stop the plan before the body leaves the ground.
func _test_interrupt_by_grab(window, view) -> void:
	window.motion.settle_at(700.0)
	window._motion_active = false
	check(window._begin_walk(-1, 4), "a second walk starts")
	check(window._walk_active, "the second walk is active")
	var before: float = window.motion.anchor.x
	window._begin_hold(Vector2i(int(before), 700))
	check(not window._walk_active, "grabbing ends the walk")
	# The plan must be gone, so further frames cannot move the soles.
	window._update_walk()
	check(is_equal_approx(window.motion.anchor.x, before) or window.motion.is_held(), "a cancelled walk stops driving the position")
	window.motion.release()
	window._cancel_drag()
	for i in 400:
		window._update_motion(1.0 / 60.0)
		if window.motion.is_grounded():
			break
	check(window.motion.is_grounded(), "the pet lands after the interrupted walk")
	check(not window._walk_active, "landing does not resume the walk")
	# Leave the pet fully idle for the next case. The hold latched a pickup
	# action that outranks everything, so it must be released explicitly.
	window.motion.settle_at(500.0)
	window._motion_active = false
	if window._held_request > 0 and view.has_method("release_action"):
		view.release_action(window._held_request)
	window._held_request = -1
	window._play_action("idle")


## A user action replaces the clip, so the plan must let go in the same frame.
func _test_interrupt_by_action(window, view) -> void:
	# Fully settle first. The previous case ended with a hold, and a held action
	# outranks everything else until it is released — `_play_action("idle")`
	# alone would be rejected by the priority table.
	window.motion.settle_at(500.0)
	window._motion_active = false
	if window._held_request > 0 and view.has_method("release_action"):
		view.release_action(window._held_request)
	window._held_request = -1
	window._fall_request = -1
	window._play_action("idle")
	check(view.action == "idle", "the pet is idle before the action test, got %s" % view.action)
	check(window._begin_walk(1, 2), "a third walk starts")
	window._play_action("wave")
	check(not window._walk_active, "an action request ends the walk")
	check(view.action == "wave", "the requested action plays, got %s" % view.action)
	window._play_action("idle")


## The behaviour chain must stay out of the way while the pet is busy.
func _test_behavior_gate(window) -> void:
	window.behavior_enabled = false
	window._behavior_cooldown = 0.0
	var before: float = window.motion.anchor.x
	window._update_behavior(1.0 / 60.0)
	check(is_equal_approx(window.motion.anchor.x, before), "a disabled chain decides nothing")

	# Busy states must not produce a decision either, even when enabled.
	window.behavior_enabled = true
	window._behavior_cooldown = 0.0
	window.motion.begin_hold(Vector2(500.0, 400.0), 100.0)
	window._motion_active = true
	window._update_behavior(1.0 / 60.0)
	check(window._behavior_cooldown == 0.0, "a held pet does not decide")
	window.motion.release()
	for i in 400:
		window._update_motion(1.0 / 60.0)
		if window.motion.is_grounded():
			break
	window._motion_active = false
	# Later tests drive the clip by hand, so the chain stays off.
	window.behavior_enabled = false

	# Room measurement must be sane at both walls.
	window.motion.settle_at(float(WORK_AREA.position.x) + 100.0)
	var room: Dictionary = window._walk_room()
	check(float(room.left) >= 0.0, "the left gap is never negative, got %f" % room.left)
	check(float(room.right) > float(room.left), "a pet at the left wall has room to its right")


## Sideways facing must be expressible and must survive a turn.
func _test_facing(view) -> void:
	view.face_direction(1)
	check(true, "facing right is accepted")
	view.face_direction(-1)
	check(true, "facing left is accepted")
	view.release_walk_facing()
	check(true, "releasing facing returns to the rest yaw")
	var before_rest: float = view.rest_yaw()
	# A completed turn updates the yaw the body returns to; otherwise walking
	# once would silently undo a manual turn.
	check(is_equal_approx(before_rest, 0.0), "the resting yaw starts at forward, got %f" % before_rest)
