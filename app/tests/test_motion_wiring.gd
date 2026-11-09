extends SceneTree
## End-to-end check of the drag -> fall -> land -> recover wiring on the shipping
## window. Interaction is injected by calling the same handlers the input loop
## uses; the native bridge is absent, which the code paths tolerate.

const PetWindow = preload("res://scripts/pet_window.gd")

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
	check(view != null, "shipping window loaded a character")
	if view == null:
		print("MOTION_WIRING_TESTS checks=", checks, " failures=", failures)
		quit(1)
		return

	# Headless runs use a tiny fake desktop, so the bounds are pinned to a real
	# one here. Everything below then measures against a known ground line.
	window.motion.set_bounds(Rect2i(0, 0, 1920, 1000), Vector2(200, 400))
	window.motion.settle_at(960.0)
	window._motion_active = true
	# A real drag lifts the body and switches to the pickup hold.
	var soles_before: Vector2 = window.motion.anchor
	window._held_request = int(view.request_action("pickup", "drag", 1.0, 1, true))
	# The grab point is on the body, 200 px above the soles. Pressing the mouse
	# must not move the pet at all: the soles stay exactly where they were, or
	# the character visibly jumps the moment the button goes down.
	window.motion.begin_hold(Vector2(soles_before.x, soles_before.y - 200.0), 200.0)
	window._motion_active = true
	view.set_ground_contact(false)
	check(window.motion.is_held(), "drag begins a hold")
	check(view.action == "pickup", "drag plays pickup, got %s" % view.action)
	for i in 10:
		window.motion.update(1.0 / 60.0)
	check(absf(window.motion.anchor.y - soles_before.y) < 1.0, "grabbing does not move the body, y=%f" % window.motion.anchor.y)

	# Holding above the ground keeps the pet up and does not sink it.
	for i in 120:
		window.motion.steer(Vector2(soles_before.x, 550.0))
		window.motion.update(1.0 / 60.0)
	check(window.motion.anchor.y < soles_before.y - 100.0, "hold lifts the body, y=%f" % window.motion.anchor.y)
	check(window.motion.anchor.y < 1000.0, "held pet is above the ground")

	# Releasing starts a fall and switches the action.
	window._end_hold()
	check(window.motion.is_falling(), "release starts a fall")
	check(view.action == "fall", "release plays fall, got %s" % view.action)

	# Let the fall finish through the real per-frame update.
	var landed := false
	for i in 400:
		window._update_motion(1.0 / 60.0)
		if window.motion.is_grounded():
			landed = true
			break
	check(landed, "the fall reaches the ground")
	check(absf(window.motion.anchor.y - 1000.0) < 1.0, "landing rests on the work area bottom, y=%f" % window.motion.anchor.y)
	check(window.motion.velocity == Vector2.ZERO, "landing stops the body")

	# A long drop must play the recovery action.
	for i in 4:
		await process_frame
	check(view.action == "recover", "a hard landing plays recover, got %s" % view.action)
	# Foot planting is deliberately held back during the crouched recovery pose;
	# it must not be on while the legs are folded under the body.
	check(not view.ground_contact_enabled(), "feet stay free during the recovery crouch")

	# A cancelled drag must not leave the pet hanging.
	window.motion.begin_hold(Vector2(960, 400), 200.0)
	window._motion_active = true
	for i in 10:
		window.motion.hold(Vector2(960, 400), 1.0 / 60.0)
	window._cancel_drag()
	check(not window.motion.is_held(), "a cancelled drag ends the hold")
	for i in 400:
		window._update_motion(1.0 / 60.0)
		if window.motion.is_grounded():
			break
	check(window.motion.is_grounded(), "a cancelled drag still falls to the ground")

	# A short drop must not play recovery, and feet must come back afterwards.
	window.motion.begin_hold(Vector2(960, 990), 10.0)
	window._motion_active = true
	window.motion.hold(Vector2(960, 990), 1.0 / 60.0)
	window._end_hold()
	for i in 60:
		window._update_motion(1.0 / 60.0)
	check(not window._recovering, "a tiny drop does not enter recovery")
	check(window.motion.is_grounded(), "the tiny drop still lands")

	# Standing still on the ground is when the feet should be planted again.
	window._on_action_changed("idle")
	window._ground_contact_delay = 0.0
	window._settle_ground_contact(1.0 / 60.0)
	check(window.current_action == "idle", "the upright pose is recognised")
	check(view.ground_contact_enabled(), "feet are re-planted once upright, got %s" % view.ground_contact_enabled())
	# And a crouched action must release them again.
	window._on_action_changed("recover")
	window._settle_ground_contact(1.0 / 60.0)
	check(not view.ground_contact_enabled(), "a crouched action frees the feet")

	# A click must never lift the pet. DragState reports `begin` on press but only
	# `moved` once the pointer travels past its threshold, and the window arms a
	# pending hold instead of grabbing immediately.
	var probe := window.drag
	probe.cancel()
	var press := Vector2i(960, 950)
	var down_state: Dictionary = probe.update(true, true, press, Vector2i(640, 300), true, true)
	check(down_state.begin, "a press reports begin")
	check(not down_state.moved, "a press alone reports no movement")
	var still: Dictionary = probe.update(true, true, press + Vector2i(1, 1), Vector2i(640, 300), true, true)
	check(not still.moved, "a 1px jitter is still not a drag")
	var lifted: Dictionary = probe.update(true, true, press + Vector2i(30, -40), Vector2i(640, 300), true, true)
	check(lifted.moved, "travel past the threshold reports movement")
	probe.cancel()

	# And a click leaves the pet standing: arming pending and releasing it must
	# not touch the motion controller at all.
	window.motion.settle_at(960.0)
	window._motion_active = false
	var rest_anchor: Vector2 = window.motion.anchor
	window._hold_pending = true
	window._press_cursor = press
	check(not window.motion.is_held(), "a press on its own does not start a hold")
	check(window.motion.anchor == rest_anchor, "a press on its own does not move the body")
	window._hold_pending = false
	check(not window.motion.is_held() and window.motion.is_grounded(),
			"a click leaves the pet standing")

	# The same press becomes a drag once the pointer moves.
	window._hold_pending = true
	window._press_cursor = press
	if window._hold_pending:
		window._hold_pending = false
		window._begin_hold(window._press_cursor)
	check(window.motion.is_held(), "travel past the threshold starts the hold")
	window._end_hold()
	for i in 400:
		window._update_motion(1.0 / 60.0)
		if window.motion.is_grounded():
			break
	check(window.motion.is_grounded(), "the drag still lands")

	print("MOTION_WIRING_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)
