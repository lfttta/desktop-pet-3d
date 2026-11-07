extends SceneTree

const CONTRACT_DIRECTORY := "res://../characters"
const MOTION_LIBRARY_PREFIX := "humanoid_core"


func _initialize() -> void:
	call_deferred("run")


## Bake targets are the contracts, not a fixed roster: every schema v2 contract
## bound to this motion library qualifies. Local-only samples stay opt-in.
func discover_characters() -> Array:
	var result: Array = []
	var directory := DirAccess.open(ProjectSettings.globalize_path(CONTRACT_DIRECTORY))
	if directory == null:
		push_error("Cannot open " + CONTRACT_DIRECTORY)
		return result
	var names: Array[String] = []
	for file in directory.get_files():
		if file.ends_with(".json") and not file.ends_with(".local.json"):
			names.append(file)
	names.sort()
	for file in names:
		var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(CONTRACT_DIRECTORY + "/" + file))
		if not parsed is Dictionary:
			continue
		var config: Dictionary = parsed
		if int(config.get("schema_version", 0)) < 2:
			continue
		if not str(config.get("motion_library", "")).begins_with(MOTION_LIBRARY_PREFIX):
			continue
		result.append(config)
	return result


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
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://../build/stage2"))
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://../animations/humanoid_core/manifest.json"))
	var actions: Array = manifest.actions.keys()
	var report := {}
	# One contract failing must not stop the remaining contracts from baking: the
	# packed character's library is written and reported before the run fails.
	var failures: Array[String] = []
	for config in discover_characters():
		var character_id := str(config.id)
		var output := "res://../build/stage2/%s.tres" % character_id
		var packed := load(str(config.model_path)) as PackedScene
		if packed == null:
			push_error("Cannot load " + character_id)
			failures.append(character_id + ": cannot load " + str(config.model_path))
			continue
		var model := packed.instantiate()
		root.add_child(model)
		await process_frame
		var target := model.get_node_or_null(str(config.skeleton_path)) as Skeleton3D
		if target == null:
			push_error("Cannot resolve skeleton for " + character_id + ": " + str(config.skeleton_path))
			failures.append(character_id + ": cannot resolve skeleton")
			model.free()
			continue
		var library := AnimationLibrary.new()
		var character_report := {}
		var character_failed := false
		for action in actions:
			# Bake from unoptimized keys, as the baseline baker does. Editor
			# scene import can simplify tracks before our fixed-rate sampling.
			var document := GLTFDocument.new()
			var state := GLTFState.new()
			if document.append_from_file("res://resources/animations/source/%s.glb" % action, state, 8) != OK:
				failures.append(character_id + ": cannot open source " + str(action))
				character_failed = true
				break
			var source_model := document.generate_scene(state)
			root.add_child(source_model)
			var source := find_first(source_model, "Skeleton3D") as Skeleton3D
			var source_player := find_first(source_model, "AnimationPlayer") as AnimationPlayer
			if source == null or source_player == null or source_player.get_animation(action) == null:
				failures.append(character_id + ": source clip " + str(action) + " is not playable")
				character_failed = true
				source_model.free()
				break
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
					failures.append(character_id + ": missing mapped bone " + required)
					character_failed = true
					break
			if character_failed:
				source_model.free()
				break
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
		if character_failed:
			# Never leave a stale library behind for a contract that failed.
			var stale := ProjectSettings.globalize_path(output)
			if FileAccess.file_exists(stale):
				DirAccess.remove_absolute(stale)
			model.free()
			continue
		if ResourceSaver.save(library, output) != OK:
			push_error("Could not save " + output)
			failures.append(character_id + ": cannot save " + output)
			model.free()
			continue
		report[character_id] = character_report
		model.free()
	print("STAGE2_BAKED ", JSON.stringify(report))
	if not failures.is_empty():
		print("STAGE2_BAKE_FAILURES ", JSON.stringify(failures))
	await process_frame
	quit(0 if failures.is_empty() else 1)
