extends SceneTree

const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]
const IDS := ["godette", "alicia", "soldier"]


func _initialize() -> void:
	call_deferred("run")


func find_first(root_node: Node, class_name_: String) -> Node:
	if root_node.is_class(class_name_):
		return root_node
	for child in root_node.get_children():
		var found := find_first(child, class_name_)
		if found:
			return found
	return null


func world_rest(skeleton: Skeleton3D, index: int) -> Transform3D:
	return skeleton.global_transform * skeleton.get_bone_global_rest(index)


func world_rotation(value: Transform3D) -> Quaternion:
	return value.basis.orthonormalized().get_rotation_quaternion().normalized()


func alignment_pairs() -> Dictionary:
	var result := {}
	for side in ["Left", "Right"]:
		for pair in [["Shoulder", "UpperArm"], ["UpperArm", "LowerArm"], ["LowerArm", "Hand"], ["Hand", "MiddleProximal"], ["UpperLeg", "LowerLeg"], ["LowerLeg", "Foot"], ["Foot", "Toes"]]:
			result[side + pair[0]] = side + pair[1]
		for finger in ["Index", "Middle", "Ring", "Little"]:
			result[side + finger + "Proximal"] = side + finger + "Intermediate"
			result[side + finger + "Intermediate"] = side + finger + "Distal"
		result[side + "ThumbMetacarpal"] = side + "ThumbProximal"
		result[side + "ThumbProximal"] = side + "ThumbDistal"
	return result


func solve_world_rotation(skeleton: Skeleton3D, index: int, desired: Dictionary, cache: Dictionary) -> Quaternion:
	if cache.has(index):
		return cache[index]
	var parent := skeleton.get_bone_parent(index)
	var parent_rotation := world_rotation(skeleton.global_transform)
	if parent >= 0:
		parent_rotation = solve_world_rotation(skeleton, parent, desired, cache)
	var value: Quaternion
	if desired.has(index):
		value = desired[index]
	else:
		value = parent_rotation * world_rotation(skeleton.get_bone_rest(index))
	cache[index] = value.normalized()
	return cache[index]


func run() -> void:
	if not FileAccess.file_exists("res://../.recovery-workspace.json"):
		push_error("Baking is restricted to the isolated recovery workspace")
		quit(6)
		return
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://../animations/humanoid_core/manifest.json"))
	var report := {}
	var selected_ids: Array = IDS
	if "--godette-only" in OS.get_cmdline_user_args():
		selected_ids = ["godette"]
	for character_id in selected_ids:
		var config: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/%s.json" % character_id))
		var packed := load(str(config.model_path)) as PackedScene
		if packed == null:
			push_error("Cannot load " + character_id)
			quit(2)
			return
		var model := packed.instantiate()
		root.add_child(model)
		await process_frame
		var target := model.get_node_or_null(str(config.skeleton_path)) as Skeleton3D
		if target == null:
			push_error("Cannot resolve skeleton for " + character_id + ": " + str(config.skeleton_path))
			quit(3)
			return
		var library := AnimationLibrary.new()
		var character_report := {}
		for action in ACTIONS:
			# Bake from unoptimized keys, as the baseline baker does. Editor
			# scene import can simplify tracks before our fixed-rate sampling.
			var document := GLTFDocument.new()
			var state := GLTFState.new()
			if document.append_from_file("res://resources/animations/source/%s.glb" % action, state, 8) != OK:
				quit(7)
				return
			var source_model := document.generate_scene(state)
			root.add_child(source_model)
			var source := find_first(source_model, "Skeleton3D") as Skeleton3D
			var source_player := find_first(source_model, "AnimationPlayer") as AnimationPlayer
			var original := source_player.get_animation(action)
			var pairs := {}
			var corrections := {}
			for profile in config.bone_map:
				if not config.source_bone_map.has(profile):
					continue
				var source_index := source.find_bone(str(config.source_bone_map[profile]))
				var target_index := target.find_bone(str(config.bone_map[profile]))
				if source_index >= 0 and target_index >= 0:
					pairs[profile] = [source_index, target_index]
					corrections[profile] = Quaternion.IDENTITY
			for required in ["Hips", "Spine", "Head", "LeftUpperArm", "RightUpperArm", "LeftUpperLeg", "RightUpperLeg", "LeftFoot", "RightFoot"]:
				if not pairs.has(required):
					push_error(character_id + " missing mapped bone " + required)
					quit(4)
					return
			var yaw := Quaternion(Vector3.UP, deg_to_rad(float(config.source_to_target_yaw_degrees)))
			var next := alignment_pairs()
			for profile in pairs:
				if not next.has(profile) or not pairs.has(next[profile]):
					continue
				var pair: Array = pairs[profile]
				var child: Array = pairs[next[profile]]
				var source_delta := world_rest(source, child[0]).origin - world_rest(source, pair[0]).origin
				var target_delta := world_rest(target, child[1]).origin - world_rest(target, pair[1]).origin
				if source_delta.length_squared() > 0.00000001 and target_delta.length_squared() > 0.00000001:
					var source_direction := yaw * source_delta.normalized()
					var target_direction := target_delta.normalized()
					corrections[profile] = Quaternion(target_direction, source_direction).normalized()
			for side in ["Left", "Right"]:
				if not pairs.has(side + "MiddleProximal") and pairs.has(side + "Hand"):
					corrections[side + "Hand"] = corrections[side + "LowerArm"]
				if pairs.has(side + "Toes"):
					corrections[side + "Toes"] = corrections[side + "Foot"]
			var source_leg := 0.0
			var target_leg := 0.0
			for side in ["Left", "Right"]:
				for segment in [["UpperLeg", "LowerLeg"], ["LowerLeg", "Foot"]]:
					var parent_pair: Array = pairs[side + segment[0]]
					var child_pair: Array = pairs[side + segment[1]]
					source_leg += world_rest(source, parent_pair[0]).origin.distance_to(world_rest(source, child_pair[0]).origin)
					target_leg += world_rest(target, parent_pair[1]).origin.distance_to(world_rest(target, child_pair[1]).origin)
			var ratio := target_leg / source_leg
			var animation := Animation.new()
			animation.resource_name = action
			animation.length = original.length
			animation.loop_mode = Animation.LOOP_LINEAR if str(manifest.actions[action].mode) == "loop" else Animation.LOOP_NONE
			var tracks := {}
			for profile in pairs:
				var track := animation.add_track(Animation.TYPE_ROTATION_3D)
				animation.track_set_path(track, NodePath(str(config.skeleton_path) + ":" + str(config.bone_map[profile])))
				tracks[profile] = track
			var hip_track := animation.add_track(Animation.TYPE_POSITION_3D)
			animation.track_set_path(hip_track, NodePath(str(config.skeleton_path) + ":" + str(config.bone_map.Hips)))
			var frame_count := roundi(original.length * 30.0) + 1
			source_player.play(action)
			for frame in frame_count:
				var time := minf(frame / 30.0, original.length)
				source_player.seek(time, true)
				source.force_update_all_bone_transforms()
				var desired := {}
				var rotations := {}
				for profile in pairs:
					var source_index: int = pairs[profile][0]
					var target_index: int = pairs[profile][1]
					var source_pose := world_rotation(source.global_transform * source.get_bone_global_pose(source_index))
					var delta := source_pose * world_rotation(world_rest(source, source_index)).inverse()
					desired[target_index] = (yaw * delta * yaw.inverse() * corrections[profile] * world_rotation(world_rest(target, target_index))).normalized()
				for index in target.get_bone_count():
					solve_world_rotation(target, index, desired, rotations)
				for profile in pairs:
					var target_index: int = pairs[profile][1]
					var parent := target.get_bone_parent(target_index)
					var parent_rotation: Quaternion = rotations[parent] if parent >= 0 else world_rotation(target.global_transform)
					var desired_local: Quaternion = (parent_rotation.inverse() * Quaternion(rotations[target_index])).normalized()
					# Godot 4 pose tracks include Bone Rest; they are not additive deltas.
					animation.rotation_track_insert_key(tracks[profile], time, desired_local)
				var hip_pair: Array = pairs.Hips
				var delta_position := (source.global_transform * source.get_bone_global_pose(hip_pair[0])).origin - world_rest(source, hip_pair[0]).origin
				var hip_parent := target.get_bone_parent(hip_pair[1])
				var parent_world := target.global_transform if hip_parent < 0 else world_rest(target, hip_parent)
				var hip_position := parent_world.affine_inverse() * (world_rest(target, hip_pair[1]).origin + yaw * delta_position * ratio)
				animation.position_track_insert_key(hip_track, time, hip_position / target.motion_scale)
			library.add_animation(action, animation)
			character_report[action] = {"length": animation.length, "frames": frame_count, "tracks": animation.get_track_count()}
			source_model.free()
		var output := "res://resources/animations/%s.tres" % character_id
		if ResourceSaver.save(library, output) != OK:
			push_error("Could not save " + output)
			quit(5)
			return
		report[character_id] = character_report
		model.free()
	print("STAGE2_BAKED ", JSON.stringify(report))
	await process_frame
	quit(0)
