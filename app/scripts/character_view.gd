extends Node3D
## The model's node names and facial channels are contained by this adapter.

signal action_changed(action: String)
const SkinBounds = preload("res://scripts/skin_bounds.gd")

var config := {}
var model: Node3D
var skeleton: Skeleton3D
var player: AnimationPlayer
var meshes: Array[MeshInstance3D] = []
var spring_controllers: Array[Node] = []
var bone_map := {}
var action := "idle"
var face_clock := 0.0
var mouth_demo := false
var blink_override := -1.0
var mouth_override := -1.0
var _last_spring_pose := {}
var spring_motion_max := 0.0
var _head_height := 0.3
var _skin_bounds := SkinBounds.new()

func load_character(entry: Dictionary) -> bool:
	var path := str(entry.get("config", ""))
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path)) if FileAccess.file_exists(path) else null
	if not parsed is Dictionary:
		push_error("Missing character configuration: " + path)
		return false
	config = parsed
	var packed := load(str(config.get("model_path", ""))) as PackedScene
	var library := load(str(config.get("animation_library", ""))) as AnimationLibrary
	if packed == null or library == null or not library.has_animation("idle") or not library.has_animation("wave"):
		push_error("Character model or shared action cache is incomplete: " + path)
		return false
	model = packed.instantiate() as Node3D
	if model == null:
		return false
	model.name = "Model"
	add_child(model)
	skeleton = model.get_node_or_null(str(config.get("skeleton_path", ""))) as Skeleton3D
	if skeleton == null:
		push_error("Character has no mapped Skeleton3D")
		return false
	bone_map = config.get("bone_map", {})
	for required in ["Hips", "Head", "LeftUpperArm", "RightUpperArm", "LeftFoot", "RightFoot"]:
		if not bone_map.has(required) or skeleton.find_bone(str(bone_map[required])) < 0:
			push_error("Missing essential humanoid bone: " + required)
			return false
	_collect(model)
	for node in model.find_children("*", "AnimationPlayer", true, false):
		node.stop()
		node.active = false
	for hidden_path in config.get("hide_mesh_paths", []):
		var hidden_mesh := model.get_node_or_null(str(hidden_path)) as GeometryInstance3D
		if hidden_mesh:
			hidden_mesh.visible = false
	# Skeleton modifiers restore their input pose after evaluation, so sampling
	# in this adapter's ordinary _process would incorrectly report no motion.
	skeleton.skeleton_updated.connect(_observe_secondary_motion)
	var display: Dictionary = config.get("display", {})
	var minimum: Array = display.get("rest_bounds_min", [0, 0, 0])
	var maximum: Array = display.get("rest_bounds_max", [0, 1.6, 0])
	var height := maxf(0.1, float(maximum[1]) - float(minimum[1]))
	var factor := 1.6 / height
	scale = Vector3.ONE * factor
	position.y = float(display.get("ground_offset_y", 0)) * factor
	var angles: Array = display.get("rotation_degrees", [0, 0, 0])
	rotation_degrees = Vector3(float(angles[0]), float(angles[1]), float(angles[2]))
	var head_in_model := global_transform.affine_inverse() * _bone_position("Head")
	_head_height = clampf((float(maximum[1]) - head_in_model.y) * factor, 0.16, 0.6)
	player = AnimationPlayer.new()
	player.name = "SharedActions"
	model.add_child(player)
	player.root_node = NodePath("..")
	# The supplied short wave cache loops. A click greeting plays it once;
	# never mutate the cached/imported asset shared by other character views.
	var runtime_library := library.duplicate() as AnimationLibrary
	var greeting := library.get_animation("wave").duplicate() as Animation
	greeting.loop_mode = Animation.LOOP_NONE
	runtime_library.remove_animation("wave")
	runtime_library.add_animation("wave", greeting)
	player.add_animation_library("shared", runtime_library)
	player.animation_finished.connect(_animation_finished)
	play_action("idle")
	player.advance(0)
	_skin_bounds.configure(meshes, skeleton)
	_skin_bounds.sample(skeleton, true)
	# Draw bounds are deliberately generous so a raised hand is not culled.
	for mesh in meshes:
		mesh.extra_cull_margin = 1.0
	print("[character] loaded id=", config.get("id"), " bones=", skeleton.get_bone_count(), " springs=", spring_controllers.size())
	return true

func _collect(root: Node) -> void:
	if root is MeshInstance3D:
		meshes.append(root)
	var script: Variant = root.get_script()
	if script is Script and script.get_global_name() == "VRMSpringBoneController":
		spring_controllers.append(root)
	for child in root.get_children():
		_collect(child)

func play_action(name_: String) -> void:
	if player == null or not player.has_animation("shared/" + name_):
		return
	action = name_
	player.play("shared/" + name_, 0.15)
	action_changed.emit(action)

func _animation_finished(name_: StringName) -> void:
	if name_ == &"shared/wave":
		play_action("idle")

func set_expression(name_: String, weight: float) -> void:
	for binding in config.get("expressions", {}).get(name_, []):
		var mesh := model.get_node_or_null(str(binding.get("mesh_path", ""))) as MeshInstance3D
		if mesh and int(binding.get("index", -1)) >= 0 and int(binding.index) < mesh.get_blend_shape_count():
			mesh.set_blend_shape_value(int(binding.index), clampf(weight * float(binding.get("weight", 1)), 0, 1))

func _process(delta: float) -> void:
	face_clock += delta
	var phase := fmod(face_clock, 4.5)
	var blink := maxf(0, 1.0 - absf(phase - 0.13) / 0.13) if phase < 0.26 else 0.0
	set_expression("blink", blink if blink_override < 0 else blink_override)
	var mouth := (0.5 + 0.5 * sin(face_clock * 12.0)) * 0.8 if mouth_demo else 0.0
	set_expression("mouth_a", mouth if mouth_override < 0 else mouth_override)

func _observe_secondary_motion() -> void:
	# Observe the final post-modifier pose; the plugin owns the simulation.
	if skeleton:
		_skin_bounds.sample(skeleton, false, 33333 if action == "wave" else 100000)
		for i in skeleton.get_bone_count():
			var name_ := skeleton.get_bone_name(i)
			if "hair" in name_.to_lower() or "skirt" in name_.to_lower():
				var pose := skeleton.get_bone_pose_rotation(i)
				if _last_spring_pose.has(i):
					spring_motion_max = maxf(spring_motion_max, pose.angle_to(_last_spring_pose[i]))
				_last_spring_pose[i] = pose

func project_visible_rect(camera: Camera3D, refresh := false) -> Rect2:
	# Normal frames read the last post-modifier sample; only explicit probes
	# force a fresh sample outside the skeleton-updated callback.
	var box: AABB = _skin_bounds.sample(skeleton, true) if refresh else _skin_bounds.cached
	var rectangle := Rect2(camera.unproject_position(box.position), Vector2.ZERO)
	for i in 8:
		rectangle = rectangle.expand(camera.unproject_position(box.get_endpoint(i)))
	# Keep the one-pixel raster/outline fringe visible at a desktop edge.
	return rectangle.grow(1.0)

func _bone_position(profile: String) -> Vector3:
	var index := skeleton.find_bone(str(bone_map.get(profile, "")))
	if index < 0:
		return global_position
	return skeleton.global_transform * skeleton.get_bone_global_pose(index).origin

func project_hit_regions(camera: Camera3D) -> Array:
	var result: Array = []
	var center := _bone_position("Hips")
	var pixel_scale := camera.unproject_position(center).distance_to(camera.unproject_position(center + Vector3.UP))
	var chest := "Chest" if bone_map.has("Chest") else "Spine"
	_add_region(result, camera, "Hips", chest, 0.16 * pixel_scale)
	_add_region(result, camera, chest, "Head", 0.10 * pixel_scale)
	# A head joint commonly sits at the neck, especially in stylized models.
	# Fit the visible head height instead of treating that joint as its center.
	var head := _bone_position("Head")
	result.append({"a": camera.unproject_position(head + Vector3.UP * _head_height * 0.3), "b": camera.unproject_position(head + Vector3.UP * _head_height * 0.7), "radius": _head_height * 0.33 * pixel_scale})
	for side in ["Left", "Right"]:
		_add_region(result, camera, side + "Shoulder", side + "UpperArm", 0.065 * pixel_scale)
		_add_region(result, camera, side + "UpperArm", side + "LowerArm", 0.065 * pixel_scale)
		_add_region(result, camera, side + "LowerArm", side + "Hand", 0.065 * pixel_scale)
		_add_region(result, camera, side + "UpperLeg", side + "LowerLeg", 0.09 * pixel_scale)
		_add_region(result, camera, side + "LowerLeg", side + "Foot", 0.075 * pixel_scale)
		_add_region(result, camera, side + "Foot", side + "Toes", 0.065 * pixel_scale)
	return result

func _add_region(regions: Array, camera: Camera3D, start: String, end: String, radius: float) -> void:
	if bone_map.has(start) and bone_map.has(end):
		regions.append({"a": camera.unproject_position(_bone_position(start)), "b": camera.unproject_position(_bone_position(end)), "radius": radius})

func describe() -> Dictionary:
	var positions := {}
	if skeleton:
		for profile in ["Hips", "Head", "LeftHand", "RightHand", "LeftFoot", "RightFoot"]:
			var point := _bone_position(profile)
			positions[profile] = [point.x, point.y, point.z]
	return {"id": config.get("id"), "bone_count": skeleton.get_bone_count() if skeleton else 0, "action": action, "animation_time": player.current_animation_position if player and player.is_playing() else 0.0, "expressions": config.get("expressions", {}).keys(), "spring_controllers": spring_controllers.size(), "observed_secondary_rotation_radians": spring_motion_max, "positions": positions, "bounds_samples": _skin_bounds.samples, "bounds_mean_usec": float(_skin_bounds.total_sample_usec) / maxi(1, _skin_bounds.samples)}
