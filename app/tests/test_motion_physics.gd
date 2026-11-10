extends SceneTree
## Focused tests for the drag/fall/landing physics and leg contact.
##
## The motion controller is pure arithmetic, so it is exercised without a display
## server. The leg IK is exercised against the shipped character model, because
## the correction depends on the real rest pose and bone lengths.

const MotionController = preload("res://scripts/motion/motion_controller.gd")
const LegIK = preload("res://scripts/motion/leg_ik.gd")
const Solver = preload("res://scripts/motion/two_bone_solver.gd")

var checks := 0
var failures := 0
var _observed_skeleton: Skeleton3D
var _observed_foot_index := -1
var _observed_foot: Variant = null


func _observe_skeleton(skeleton: Skeleton3D, foot_index: int) -> void:
	_observed_skeleton = skeleton
	_observed_foot_index = foot_index


func _on_skeleton_updated() -> void:
	if _observed_skeleton != null and _observed_foot_index >= 0:
		_observed_foot = _observed_skeleton.global_transform * _observed_skeleton.get_bone_global_pose(_observed_foot_index).origin


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	_test_solver()
	_run_motion_tests()
	await _test_leg_ik()
	print("MOTION_PHYSICS_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)


func _test_solver() -> void:
	var root := Vector3(0, 1, 0)
	var mid := Vector3(0, 0.5, 0)
	var end := Vector3(0, 0, 0)
	var goal := Vector3(0, 0.25, 0)
	var result: Dictionary = Solver.solve(root, mid, end, goal, Vector3(0, 0, 1))
	check(result.has("mid"), "solver returns a solution")
	check((result.end as Vector3).distance_to(goal) < 0.001, "solver puts the ankle on the target")
	check(absf((result.mid as Vector3).distance_to(root) - 0.5) < 0.001, "upper segment keeps its length")
	check(absf((result.end as Vector3).distance_to(result.mid) - 0.5) < 0.001, "lower segment keeps its length")
	check((result.mid as Vector3).z > 0.1, "knee bends toward the pole, z=%f" % (result.mid as Vector3).z)

	# Out of reach: the chain must straighten, not stretch.
	var far: Dictionary = Solver.solve(root, mid, end, Vector3(0, -5, 0), Vector3(0, 0, 1))
	check(far.reachable == false, "unreachable target is reported")
	check((far.mid as Vector3).distance_to(root) < 0.5001, "unreachable target does not stretch the upper segment")

	# Degenerate input must not produce NaN.
	var degenerate: Dictionary = Solver.solve(root, root, root, goal, Vector3.ZERO)
	check(not degenerate.reachable, "degenerate chain is rejected instead of solved")
	check(not is_nan((degenerate.mid as Vector3).x), "degenerate chain does not produce NaN")

	# Opposite vectors must still yield a finite rotation.
	var flipping: Quaternion = Solver.rotation_between(Vector3(0, 1, 0), Vector3(0, -1, 0))
	check(not is_nan(flipping.x) and absf(flipping.get_angle() - PI) < 0.01, "antiparallel vectors rotate by pi")


func _new_motion(ground := 1000.0) -> Variant:
	var controller := MotionController.new()
	controller.set_bounds(Rect2i(0, 0, 1920, 1000), Vector2(200, 400))
	controller.settle_at(960.0)
	check(absf(controller.anchor.y - ground) < 0.001, "settling puts the soles on the work area bottom")
	return controller


func _run_motion_tests() -> void:
	# 1. Settled pet stays grounded and reports rest.
	var grounded = _new_motion()
	grounded.update(1.0 / 60.0)
	check(grounded.is_grounded(), "settled pet stays grounded")
	check(grounded.is_resting(), "settled pet has no velocity")
	check(grounded.acceleration.length() < 1.0, "settled pet has no residual acceleration")

	# 2. A hold lifts the pet and follows the cursor without snapping.
	var held = _new_motion()
	var start: Vector2 = held.anchor
	held.begin_hold(Vector2(960, 600), 200.0)
	check(held.is_held(), "hold starts")
	# The follow is a damped spring, so it settles over roughly a second rather
	# than snapping; 120 frames is two seconds at 60 Hz.
	for i in 120:
		held.hold(Vector2(960, 600), 1.0 / 60.0)
	check(held.anchor.y < start.y - 200.0, "hold lifts the pet, y=%f" % held.anchor.y)
	check(absf(held.anchor.y - 800.0) < 1.0, "hold settles at the grab height above the cursor, y=%f" % held.anchor.y)
	check(held.anchor.y >= -1.0, "hold respects the top of the work area")
	check(held.velocity.length() < 1.0, "hold comes to rest instead of accelerating forever, speed=%f" % held.velocity.length())
	# It must not pass through the ground on the way up or down.
	check(held.anchor.y <= 1000.0, "hold never sinks below the ground")

	# 3. Release falls under gravity and stops exactly at the ground.
	var falling = _new_motion()
	falling.begin_hold(Vector2(960, 300), 200.0)
	for i in 30:
		falling.hold(Vector2(960, 300), 1.0 / 60.0)
		falling.update(1.0 / 60.0)
	falling.release()
	check(falling.is_falling(), "release starts a fall")
	var elapsed := 0.0
	var landed := false
	while elapsed < 5.0:
		falling.update(1.0 / 60.0)
		elapsed += 1.0 / 60.0
		if falling.take_landing():
			landed = true
			break
	check(landed, "falling pet lands")
	check(absf(falling.anchor.y - 1000.0) < 0.001, "landing stops exactly on the ground, y=%f" % falling.anchor.y)
	check(falling.velocity == Vector2.ZERO, "landing zeroes the velocity")
	check(elapsed < 2.0, "a 400 px drop resolves in under two seconds, took %f" % elapsed)

	# 4. A harder landing is reported as needing recovery, a short one is not.
	var soft = _new_motion()
	soft.begin_hold(Vector2(960, 995), 10.0)
	for i in 3:
		soft.hold(Vector2(960, 995), 1.0 / 60.0)
		soft.update(1.0 / 60.0)
	soft.release()
	for i in 60:
		soft.update(1.0 / 60.0)
		soft.take_landing()
	check(not soft.needs_recovery(), "a tiny drop does not need recovery, speed=%f" % soft.last_landing_speed())

	var hard = _new_motion()
	hard.begin_hold(Vector2(960, 100), 200.0)
	for i in 30:
		hard.hold(Vector2(960, 100), 1.0 / 60.0)
		hard.update(1.0 / 60.0)
	hard.release()
	for i in 300:
		hard.update(1.0 / 60.0)
		if hard.take_landing():
			break
	check(hard.needs_recovery(), "a long drop needs recovery, speed=%f" % hard.last_landing_speed())

	# 5. The pet never leaves the work area, even with a violent shove.
	var bounded = _new_motion()
	bounded.begin_hold(Vector2(960, 500), 200.0)
	bounded.velocity = Vector2(100000, -100000)
	bounded.update(1.0 / 60.0)
	check(bounded.anchor.x >= 100.0 - 0.001 and bounded.anchor.x <= 1820.0 + 0.001, "horizontal bound holds, x=%f" % bounded.anchor.x)
	check(bounded.anchor.y <= 1000.0 + 0.001, "vertical bound holds, y=%f" % bounded.anchor.y)

	# 6. Horizontal momentum survives a release and decays in the air.
	var glide = _new_motion()
	glide.begin_hold(Vector2(960, 300), 200.0)
	glide.velocity = Vector2(600, 0)
	glide.release()
	var before := absf(glide.velocity.x)
	for i in 5:
		glide.update(1.0 / 60.0)
	check(glide.velocity.x < before or glide.is_grounded(), "air drag slows horizontal motion")

	# 7. Inertia is finite, bounded, and points the right way.
	var inertia_test = _new_motion()
	inertia_test.begin_hold(Vector2(960, 500), 200.0)
	inertia_test.velocity = Vector2(1500, 0)
	inertia_test.update(1.0 / 60.0)
	check(inertia_test.inertia().x > 0.0, "rightward motion shoves the springs right, got %f" % inertia_test.inertia().x)
	check(inertia_test.inertia().length() < 10.0, "inertia stays bounded, got %f" % inertia_test.inertia().length())

	# 8. A zero-delta update cannot divide by zero.
	var still = _new_motion()
	still.update(0.0)
	check(not is_nan(still.acceleration.x), "zero delta does not produce NaN")

	# 9. Settling after a drag returns the pet to the ground and clears state.
	var resettle = _new_motion()
	resettle.begin_hold(Vector2(960, 200), 200.0)
	for i in 10:
		resettle.hold(Vector2(960, 200), 1.0 / 60.0)
	resettle.settle_at(500.0)
	check(resettle.is_grounded() and resettle.velocity == Vector2.ZERO, "settle_at restores a grounded state")
	check(absf(resettle.anchor.x - 500.0) < 0.001 and absf(resettle.anchor.y - 1000.0) < 0.001, "settle_at places the soles at the request")

	# 10. `place_at` keeps a soles height that is above the ground line.
	#
	# A restored placement can leave the pet standing well above the bottom edge
	# (`placement.y < 1`). Aligning the controller used to go through `settle_at`
	# and drop that height, so touching a floating pet teleported it a full body
	# height down. The packaged desktop check measured 382 px on mouse-down.
	var floating = _new_motion()
	floating.place_at(Vector2(700.0, 600.0))
	check(floating.is_grounded(), "place_at leaves the pet standing")
	check(absf(floating.anchor.x - 700.0) < 0.001 and absf(floating.anchor.y - 600.0) < 0.001,
		"place_at keeps the requested soles height, got %s" % [floating.anchor])
	var state = _new_motion()
	state.place_at(Vector2(700.0, 600.0))
	state.begin_hold(Vector2(700.0, 400.0), 200.0)
	for i in 10:
		state.update(1.0 / 60.0)
	check(absf(state.anchor.y - 600.0) < 8.0, "grabbing a floating pet does not drop it, y=%f" % state.anchor.y)


func _test_leg_ik() -> void:
	var packed := load("res://models/godette.vrm") as PackedScene
	check(packed != null, "shipped model loads")
	if packed == null:
		return
	var model := packed.instantiate()
	root.add_child(model)
	await process_frame
	var skeleton: Skeleton3D = null
	var stack: Array = [model]
	while not stack.is_empty():
		var node: Node = stack.pop_back()
		if node is Skeleton3D:
			skeleton = node
			break
		stack.append_array(node.get_children())
	check(skeleton != null, "shipped model exposes a skeleton")
	if skeleton == null:
		model.queue_free()
		return

	var doc: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/godette.json"))
	var bone_map: Dictionary = doc.get("bone_map", {}) if doc is Dictionary else {}
	var ik = LegIK.new()
	skeleton.add_child(ik)
	ik.configure(skeleton, bone_map)
	ik.active = true
	check(ik.chain_count() == 2, "both leg chains resolve from the contract, got %d" % ik.chain_count())

	var foot_index := skeleton.find_bone("LeftFoot")
	var upper_index := skeleton.find_bone("LeftUpperLeg")
	var lower_index := skeleton.find_bone("LeftLowerLeg")
	# Modifier writes are visible during skeleton_updated, after the whole chain
	# has been evaluated; polling get_bone_global_pose from a script frame sees
	# pre-modifier values. Observe exactly like the adapter does.
	_observed_foot = null
	_observe_skeleton(skeleton, foot_index)
	skeleton.skeleton_updated.connect(_on_skeleton_updated)
	for i in 4:
		await process_frame
	var rest: Vector3 = _observed_foot
	var upper_len := (skeleton.get_bone_global_pose(upper_index).origin - skeleton.get_bone_global_pose(lower_index).origin).length()
	var lower_len := (skeleton.get_bone_global_pose(lower_index).origin - skeleton.get_bone_global_pose(foot_index).origin).length()

	# Disabled by default: the animated pose must survive untouched.
	check(not ik.is_foot_locked("Left"), "no foot is locked before configuration")
	ik.set_foot_floor("Left", rest.y + 0.2, 1.0)
	for i in 4:
		await process_frame
	check((_observed_foot - rest).length() < 0.001, "zero weight leaves the pose untouched")

	ik.set_weight(1.0)
	for i in 4:
		await process_frame
	var raised: Vector3 = _observed_foot
	check(absf(raised.y - (rest.y + 0.2)) < 0.002, "locked foot reaches the requested height, err=%f" % (raised.y - rest.y - 0.2))
	await process_frame
	check(absf(_observed_foot.y - raised.y) < 0.001, "locked foot is stable across frames")

	var legs_ok := true
	var r := skeleton.global_transform * skeleton.get_bone_global_pose(upper_index).origin
	var m := skeleton.global_transform * skeleton.get_bone_global_pose(lower_index).origin
	var e := skeleton.global_transform * skeleton.get_bone_global_pose(foot_index).origin
	if absf(r.distance_to(m) - upper_len) > 0.003 or absf(m.distance_to(e) - lower_len) > 0.003:
		legs_ok = false
	check(legs_ok, "bone lengths survive the correction")

	ik.clear_feet()
	for i in 4:
		await process_frame
	check((_observed_foot - rest).length() < 0.001, "releasing restores the animated pose")
	model.queue_free()
