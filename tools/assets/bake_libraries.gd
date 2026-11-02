extends SceneTree
## Run ONLY in tests/assets. Writes the agreed per-target AnimationLibrary caches.
## No source positions are copied to non-hip joints: target lengths remain exact.

const BASE := "D:/danjigame/3D-pet-gtpdo"
const STAGE := BASE + "/prototypes/stage0"

func _initialize() -> void:
	call_deferred("run")

func find_node(n: Node, cls: String) -> Node:
	if n.is_class(cls): return n
	for child in n.get_children():
		var found := find_node(child, cls)
		if found: return found
	return null

func load_model(path: String) -> Node3D:
	if path.begins_with("res://"): path = STAGE + "/" + path.trim_prefix("res://")
	if path.ends_with(".vrm"):
		return load(BASE+"/tools/assets/runtime_vrm_loader.gd").load_vrm(path)
	var doc := GLTFDocument.new()
	var state := GLTFState.new()
	if doc.append_from_file(path, state, 8) != OK: return null
	return doc.generate_scene(state)

func world_rest(sk: Skeleton3D, index: int) -> Transform3D:
	return sk.global_transform * sk.get_bone_global_rest(index)

func world_rotation(t: Transform3D) -> Quaternion:
	return t.basis.orthonormalized().get_rotation_quaternion().normalized()

func alignment_pairs() -> Dictionary:
	var result := {}
	for side in ["Left", "Right"]:
		for pair in [["Shoulder","UpperArm"],["UpperArm","LowerArm"],["LowerArm","Hand"],["Hand","MiddleProximal"],["UpperLeg","LowerLeg"],["LowerLeg","Foot"],["Foot","Toes"]]:
			result[side+pair[0]] = side+pair[1]
		for finger in ["Index","Middle","Ring","Little"]:
			result[side+finger+"Proximal"] = side+finger+"Intermediate"
			result[side+finger+"Intermediate"] = side+finger+"Distal"
		result[side+"ThumbMetacarpal"] = side+"ThumbProximal"
		result[side+"ThumbProximal"] = side+"ThumbDistal"
	return result

func solve_rotation(index: int, sk: Skeleton3D, desired: Dictionary, rotations: Dictionary) -> Quaternion:
	if rotations.has(index): return rotations[index]
	var parent := sk.get_bone_parent(index)
	var parent_rotation := world_rotation(sk.global_transform)
	if parent >= 0: parent_rotation = solve_rotation(parent, sk, desired, rotations)
	var q: Quaternion
	if desired.has(index):
		q = desired[index]
	else:
		q = parent_rotation * world_rotation(sk.get_bone_rest(index))
	rotations[index] = q.normalized()
	return rotations[index]

func run() -> void:
	var summaries := {}
	for id in ["godette", "swordsman", "avatar_sample_m"]:
		var config_path: String = STAGE+"/assets_data/"+id+(".local" if id=="avatar_sample_m" else "")+".json"
		var config: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(config_path))
		var model := load_model(config.model_path)
		if model == null:
			push_error("Cannot load " + id); quit(2); return
		root.add_child(model)
		var target: Skeleton3D = model.get_node(config.skeleton_path)
		var yaw := Quaternion(Vector3.UP, deg_to_rad(float(config.source_to_target_yaw_degrees)))
		var library := AnimationLibrary.new()
		var summary := {}
		for clip in ["idle", "wave"]:
			var source_model := load_model("res://motions/"+clip+".glb")
			root.add_child(source_model)
			var source: Skeleton3D = find_node(source_model,"Skeleton3D")
			var player: AnimationPlayer = find_node(source_model,"AnimationPlayer")
			var original := player.get_animation(clip)
			var pairs := {}
			var corrections := {}
			for profile in config.bone_map:
				var ti := target.find_bone(config.bone_map[profile])
				var si := source.find_bone(config.source_bone_map[profile])
				if ti < 0 or si < 0:
					push_error("Missing mapped bone " + profile); quit(3); return
				pairs[profile] = [si,ti]
				corrections[profile] = Quaternion.IDENTITY
			var next := alignment_pairs()
			for profile in pairs:
				if not next.has(profile) or not pairs.has(next[profile]): continue
				var p: Array = pairs[profile]
				var child: Array = pairs[next[profile]]
				var src_dir := yaw * (world_rest(source,child[0]).origin-world_rest(source,p[0]).origin).normalized()
				var tgt_dir := (world_rest(target,child[1]).origin-world_rest(target,p[1]).origin).normalized()
				corrections[profile] = Quaternion(tgt_dir,src_dir).normalized()
			for side in ["Left","Right"]:
				if not pairs.has(side+"MiddleProximal") and pairs.has(side+"Hand"):
					corrections[side+"Hand"] = corrections[side+"LowerArm"]
				if pairs.has(side+"Toes"): corrections[side+"Toes"] = corrections[side+"Foot"]
			var src_leg := 0.0; var tgt_leg := 0.0
			for side in ["Left","Right"]:
				for segment in [["UpperLeg","LowerLeg"],["LowerLeg","Foot"]]:
					var p: Array = pairs[side+segment[0]]; var c: Array = pairs[side+segment[1]]
					src_leg += world_rest(source,p[0]).origin.distance_to(world_rest(source,c[0]).origin)
					tgt_leg += world_rest(target,p[1]).origin.distance_to(world_rest(target,c[1]).origin)
			var ratio := tgt_leg / src_leg
			var animation := Animation.new()
			animation.resource_name = clip
			animation.length = original.length
			animation.loop_mode = Animation.LOOP_LINEAR
			var tracks := {}
			for profile in pairs:
				var track := animation.add_track(Animation.TYPE_ROTATION_3D)
				animation.track_set_path(track,NodePath(config.skeleton_path+":"+config.bone_map[profile]))
				tracks[profile] = track
			var hip_track := animation.add_track(Animation.TYPE_POSITION_3D)
			animation.track_set_path(hip_track,NodePath(config.skeleton_path+":"+config.bone_map.Hips))
			var frame_count := roundi(original.length*30.0)+1
			player.play(clip)
			for frame in frame_count:
				var time := minf(frame/30.0,original.length)
				player.seek(time,true)
				source.force_update_all_bone_transforms()
				var desired := {}; var rotations := {}
				for profile in pairs:
					var si: int = pairs[profile][0]; var ti: int = pairs[profile][1]
					var src_pose := world_rotation(source.global_transform*source.get_bone_global_pose(si))
					var delta := src_pose*world_rotation(world_rest(source,si)).inverse()
					desired[ti] = (yaw*delta*yaw.inverse()*corrections[profile]*world_rotation(world_rest(target,ti))).normalized()
				for i in target.get_bone_count(): solve_rotation(i,target,desired,rotations)
				for profile in pairs:
					var ti: int = pairs[profile][1]
					var parent := target.get_bone_parent(ti)
					var parent_rotation: Quaternion = rotations[parent] if parent >= 0 else world_rotation(target.global_transform)
					var local: Quaternion = (parent_rotation.inverse()*rotations[ti]).normalized()
					animation.rotation_track_insert_key(tracks[profile],time,local)
				var hip: Array = pairs.Hips
				var delta_position := (source.global_transform*source.get_bone_global_pose(hip[0])).origin-world_rest(source,hip[0]).origin
				# Hips' parent is an unmapped fixed rig root on all selected targets.
				var parent := target.get_bone_parent(hip[1])
				var parent_world := target.global_transform
				if parent >= 0: parent_world = world_rest(target,parent)
				var hip_position := parent_world.affine_inverse()*(world_rest(target,hip[1]).origin+yaw*delta_position*ratio)
				# Godot scales Skeleton3D position tracks by motion_scale on playback.
				# VRM importer sets this to humanoid hip height; GLB defaults to 1.
				animation.position_track_insert_key(hip_track,time,hip_position/target.motion_scale)
			library.add_animation(clip,animation)
			summary[clip] = {"length":animation.length,"frames":frame_count,"rotation_tracks":pairs.size(),"position_tracks":1,"hip_motion_scale":ratio,"godot_skeleton_motion_scale":target.motion_scale}
			source_model.free()
		var output: String = config.animation_library
		if output.begins_with("res://"): output = STAGE+"/"+output.trim_prefix("res://")
		var err := ResourceSaver.save(library,output)
		if err != OK:
			push_error("Could not save " + output); quit(4); return
		summaries[id] = summary
		print("BAKED ",id," ",JSON.stringify(summary))
		model.free()
	FileAccess.open(BASE+"/animations/cache_manifest.json",FileAccess.WRITE).store_string(JSON.stringify(summaries,"\t"))
	await process_frame
	quit(0)
