extends SceneTree
## Exercise the shipped skeleton at a fixed animation time, after modifiers.

const Character = preload("res://scripts/character_view.gd")
const LEG_BONES := ["LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "LeftToes",
	"RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes"]
var checks := 0
var failures := 0
var observed := {}

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	call_deferred("run")

func _observe(view) -> void:
	observed.clear()
	for name in LEG_BONES:
		observed[name] = view.skeleton.get_bone_global_pose(view.skeleton.find_bone(name))

func _sample(view, clip: String, time: float) -> Dictionary:
	view.player.play("shared/" + clip, 0.0)
	view.player.seek(time, true)
	view.player.advance(0.0)
	for i in 3:
		await process_frame
	return observed.duplicate()

func _same_pose(actual: Dictionary, expected: Dictionary, label: String) -> void:
	var distance := 0.0
	var angle := 0.0
	for name in LEG_BONES:
		var a: Transform3D = actual[name]
		var b: Transform3D = expected[name]
		distance = maxf(distance, a.origin.distance_to(b.origin))
		angle = maxf(angle, a.basis.get_rotation_quaternion().angle_to(b.basis.get_rotation_quaternion()))
	check(distance < 0.0001, "%s position error=%f" % [label, distance])
	check(angle < 0.001, "%s rotation error=%f" % [label, angle])

func run() -> void:
	var view = Character.new()
	root.add_child(view)
	check(view.load_character({"config": "res://assets_data/godette.json"}), "shipped character loads")
	view.set_process(false)
	view.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
	view.skeleton.skeleton_updated.connect(_observe.bind(view))
	for yaw in [0.0, PI * 0.5, PI, PI * 1.5]:
		view.rotation.y = yaw
		view.leg_ik.reset_overrides()
		view.set_ground_contact(false)
		var idle := await _sample(view, "idle", 0.25)
		view.set_ground_contact(true)
		var locked := await _sample(view, "idle", 0.25)
		_same_pose(locked, idle, "locking current pose at yaw %.2f is a no-op" % yaw)
		# A reachable raised floor should move the ankle without tilting its shoe.
		var space: Transform3D = view.skeleton.global_transform
		var base_foot: Transform3D = idle.LeftFoot
		var target: float = (space * base_foot.origin).y + 0.025
		view.leg_ik.set_foot_floor("Left", target)
		var raised := await _sample(view, "idle", 0.25)
		var raised_foot: Transform3D = raised.LeftFoot
		check(absf((space * raised_foot.origin).y - target) < 0.002, "floor reached at yaw %.2f" % yaw)
		check(base_foot.basis.get_rotation_quaternion().angle_to(raised_foot.basis.get_rotation_quaternion()) < 0.001, "shoe orientation preserved at yaw %.2f" % yaw)
		# This is the runtime disable API, not a manual skeleton reset.
		view.set_ground_contact(false)
		_same_pose(await _sample(view, "idle", 0.25), idle, "disable restores idle at yaw %.2f" % yaw)
		var walked := await _sample(view, "walk", 0.3)
		view.leg_ik.reset_overrides()
		_same_pose(walked, await _sample(view, "walk", 0.3), "walk contains no leftover correction at yaw %.2f" % yaw)
	# The same clip must remain deterministic after repeated idle/walk transitions.
	for i in 4:
		view.set_ground_contact(false)
		var clean := await _sample(view, "walk", 0.6)
		await _sample(view, "idle", 0.25)
		view.set_ground_contact(true)
		await _sample(view, "idle", 0.5)
		view.set_ground_contact(false)
		_same_pose(await _sample(view, "walk", 0.6), clean, "idle/walk transition %d" % i)
	# Runtime FK is independent of the resource sampler: the supporting foot
	# should stay almost still once the model's cycle travel is added.
	view.set_ground_contact(false)
	view.rotation.y = PI
	var stride: float = view.measure_walk_stride()
	var length: float = view.walk_cycle_length()
	for entry in [["LeftFoot", 0.3, 0.4], ["RightFoot", 0.8, 0.9]]:
		var start := await _sample(view, "walk", entry[1])
		var end := await _sample(view, "walk", entry[2])
		var a: Transform3D = start[entry[0]]
		var b: Transform3D = end[entry[0]]
		var animation_travel: float = (b.origin.z - a.origin.z) * view.skeleton.global_transform.basis.get_scale().z
		var window_travel: float = stride * (entry[2] - entry[1]) / length
		check(absf(animation_travel - window_travel) < 0.02, "%s planted-foot drift=%f" % [entry[0], absf(animation_travel - window_travel)])
	print("GAIT measured full-cycle travel=", stride)
	# A manual about-face must not invert subsequent left/right walking.
	for resting in [PI, TAU]:
		view._rest_yaw = resting
		for direction in [-1, 1]:
			view.face_direction(direction)
			view._apply_walk_yaw(1.0)
			var pose := await _sample(view, "idle", 0.25)
			var foot: Transform3D = pose.LeftFoot
			var toes: Transform3D = pose.LeftToes
			var axis: Vector3 = view.skeleton.global_transform.basis * (toes.origin - foot.origin)
			check(axis.x * direction > 0.0, "toes face walking direction after rest yaw %.2f" % resting)
	view.queue_free()
	await process_frame
	print("LEG_CONTACT_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)
