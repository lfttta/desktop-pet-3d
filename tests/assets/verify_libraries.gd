extends SceneTree
## Independent playback/skin validation of saved caches, not the baker's working data.
const BASE := "D:/danjigame/3D-pet-gtpdo"
const STAGE := BASE+"/prototypes/stage0"
var failures: Array[String] = []

func _initialize() -> void: call_deferred("run")

func check(condition: bool, message: String) -> void:
	if not condition: failures.append(message); push_error(message)

func collect(n: Node, cls: String, output: Array) -> void:
	if n.is_class(cls): output.append(n)
	for child in n.get_children(): collect(child,cls,output)

func vec(v: Vector3) -> Array: return [v.x,v.y,v.z]

func load_model(path: String) -> Node3D:
	if path.begins_with("res://"): path = STAGE+"/"+path.trim_prefix("res://")
	if path.ends_with(".vrm"): return load(BASE+"/tools/assets/runtime_vrm_loader.gd").load_vrm(path)
	var doc := GLTFDocument.new(); var state := GLTFState.new()
	if doc.append_from_file(path,state,8) != OK: return null
	return doc.generate_scene(state)

func bone_position(sk: Skeleton3D, index: int) -> Vector3:
	return (sk.global_transform*sk.get_bone_global_pose(index)).origin

func export_pose(model: Node3D, sk: Skeleton3D, config: Dictionary, path: String) -> Dictionary:
	var file := FileAccess.open(path,FileAccess.WRITE)
	var meshes := []; collect(model,"MeshInstance3D",meshes)
	var offset := 1; var count := 0
	var bounds := AABB(); var first := true
	var yaw := Basis(Vector3.UP,deg_to_rad(float(config.display.rotation_degrees[1])))
	for mi: MeshInstance3D in meshes:
		if str(model.get_path_to(mi)) in config.get("hide_mesh_paths",[]): continue
		for surface in mi.mesh.get_surface_count():
			var arrays := mi.mesh.surface_get_arrays(surface)
			var vertices: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
			var bones = arrays[Mesh.ARRAY_BONES]; var weights = arrays[Mesh.ARRAY_WEIGHTS]
			var transforms := []
			if mi.skin:
				for bind in mi.skin.get_bind_count():
					var b := sk.find_bone(mi.skin.get_bind_name(bind))
					if b < 0: b = mi.skin.get_bind_bone(bind)
					check(b >= 0 and b < sk.get_bone_count(),"Skin bind missing: "+str(mi.skin.get_bind_name(bind)))
					# RenderingServer supplies skeleton-local pose*bind; mesh instance
					# model transform is applied by the renderer afterwards.
					transforms.append(mi.global_transform*sk.get_bone_global_pose(b)*mi.skin.get_bind_pose(bind))
			var influences: int = weights.size()/vertices.size() if weights != null and vertices.size()>0 else 0
			for v in vertices.size():
				var p: Vector3 = mi.global_transform*vertices[v]
				if influences>0 and not transforms.is_empty():
					p = Vector3.ZERO
					for w in influences:
						var weight: float = weights[v*influences+w]
						if weight>0: p += (transforms[bones[v*influences+w]]*vertices[v])*weight
				p = yaw*p+Vector3(0,float(config.display.ground_offset_y),0)
				check(p.is_finite(),"Nonfinite skin vertex")
				if first: bounds=AABB(p,Vector3.ZERO); first=false
				else: bounds=bounds.expand(p)
				file.store_line("v %.8f %.8f %.8f" % [p.x,p.y,p.z])
			var indices = arrays[Mesh.ARRAY_INDEX]
			if indices != null and indices.size()>0:
				for i in range(0,indices.size(),3):
					file.store_line("f %d %d %d" % [offset+indices[i],offset+indices[i+1],offset+indices[i+2]])
			offset += vertices.size(); count += vertices.size()
	file.close()
	return {"vertices":count,"bounds_min":vec(bounds.position),"bounds_size":vec(bounds.size)}

func run() -> void:
	var results := {}
	for id in ["godette","swordsman","avatar_sample_m"]:
		var config_path: String = STAGE+"/assets_data/"+id+(".local" if id=="avatar_sample_m" else "")+".json"
		var config: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(config_path))
		var model := load_model(config.model_path)
		check(model!=null,"Missing model "+id)
		if model == null: quit(2); return
		root.add_child(model)
		var sk: Skeleton3D = model.get_node(config.skeleton_path)
		var players := []; collect(model,"AnimationPlayer",players)
		for old: AnimationPlayer in players: old.stop(); old.active=false
		var modifiers := []; collect(model,"SkeletonModifier3D",modifiers)
		for modifier in modifiers: modifier.active=false
		var ap := AnimationPlayer.new(); ap.name="ValidationPlayer"; model.add_child(ap)
		var lib_path: String = config.animation_library
		if lib_path.begins_with("res://"): lib_path=STAGE+"/"+lib_path.trim_prefix("res://")
		check(FileAccess.file_exists(lib_path),"Missing library: "+lib_path)
		if not FileAccess.file_exists(lib_path): quit(3); return
		var library: AnimationLibrary = load(lib_path)
		check(PackedStringArray(library.get_animation_list())==PackedStringArray(["idle","wave"]),"Unexpected clip set")
		ap.add_animation_library("shared",library)
		var record := {"clips":{},"expressions":{},"bone_count":sk.get_bone_count()}
		for clip in ["idle","wave"]:
			sk.reset_bone_poses()
			var anim := library.get_animation(clip)
			for t in anim.get_track_count():
				var path := anim.track_get_path(t)
				check(model.get_node_or_null(NodePath(path.get_concatenated_names()))==sk,"Track skeleton resolution failed")
				check(sk.find_bone(path.get_concatenated_subnames())>=0,"Track bone resolution failed")
				check(anim.track_get_type(t) in [Animation.TYPE_POSITION_3D,Animation.TYPE_ROTATION_3D],"Unexpected stretch/scale track")
				if anim.track_get_type(t)==Animation.TYPE_POSITION_3D: check(path.get_concatenated_subnames()==config.bone_map.Hips,"Non-hip translation track")
			ap.play("shared/"+clip)
			var frame_count := roundi(anim.length*30)+1
			var max_length_error := 0.0; var max_local_offset_error := 0.0; var max_hip_height_error := 0.0
			var hand_min := Vector3(INF,INF,INF); var hand_max := Vector3(-INF,-INF,-INF)
			var hand_above_shoulder := -INF
			var hand_index := sk.find_bone(config.bone_map.RightHand)
			var shoulder := sk.find_bone(config.bone_map.RightUpperArm)
			var samples := []
			for f in frame_count:
				var time := minf(f/30.0,anim.length)
				ap.seek(time,true); sk.force_update_all_bone_transforms()
				var hips := sk.find_bone(config.bone_map.Hips)
				max_hip_height_error=maxf(max_hip_height_error,absf(bone_position(sk,hips).y-(sk.global_transform*sk.get_bone_global_rest(hips)).origin.y))
				for b in sk.get_bone_count():
					if sk.get_bone_name(b)==config.bone_map.Hips: continue
					max_local_offset_error=maxf(max_local_offset_error,sk.get_bone_pose_position(b).distance_to(sk.get_bone_rest(b).origin))
					check(sk.get_bone_pose_scale(b).is_equal_approx(sk.get_bone_rest(b).basis.get_scale()),"Bone scale changed")
					var parent := sk.get_bone_parent(b)
					if parent < 0: continue
					var rest_length := sk.get_bone_global_rest(b).origin.distance_to(sk.get_bone_global_rest(parent).origin)
					var pose_length := sk.get_bone_global_pose(b).origin.distance_to(sk.get_bone_global_pose(parent).origin)
					max_length_error=maxf(max_length_error,absf(pose_length-rest_length))
				var hand := bone_position(sk,hand_index)
				hand_min=hand_min.min(hand); hand_max=hand_max.max(hand)
				hand_above_shoulder=maxf(hand_above_shoulder,hand.y-bone_position(sk,shoulder).y)
				if f in [0,frame_count/2,frame_count-1]:
					var joints := {}
					for key in config.bone_map: joints[key]=vec(bone_position(sk,sk.find_bone(config.bone_map[key])))
					samples.append({"time":time,"joints":joints})
			check(max_length_error<0.00002,id+" "+clip+" changed bone lengths")
			check(max_local_offset_error<0.000002,id+" "+clip+" changed non-hip offsets")
			check(max_hip_height_error<0.025,id+" "+clip+" hip height drift (motion_scale mismatch)")
			check((hand_max-hand_min).length()>0.001,id+" "+clip+" is static")
			var forearm := sk.find_bone(config.bone_map.RightLowerArm)
			var arm_length := sk.get_bone_global_rest(hand_index).origin.distance_to(sk.get_bone_global_rest(forearm).origin)+sk.get_bone_global_rest(forearm).origin.distance_to(sk.get_bone_global_rest(shoulder).origin)
			if clip=="wave": check(hand_above_shoulder>arm_length*0.15,id+" wave hand not raised relative to character arm length")
			if clip=="idle": check(hand_above_shoulder<0.0,id+" idle hand not lowered")
			ap.seek(anim.length*0.4,true); sk.force_update_all_bone_transforms()
			var folder: String = "res://local_only/" if id=="avatar_sample_m" else "res://results/"
			var mesh_stats := export_pose(model,sk,config,folder+id+"_"+clip+".obj")
			record.clips[clip]={"frames_checked":frame_count,"max_bone_length_error_m":max_length_error,"max_nonhip_offset_error_m":max_local_offset_error,"max_hip_height_drift_m":max_hip_height_error,"right_hand_travel_m":(hand_max-hand_min).length(),"right_hand_above_shoulder_m":hand_above_shoulder,"samples":samples,"skin":mesh_stats}
		for expression in config.expressions:
			var checked := []
			for bind in config.expressions[expression]:
				var mesh: MeshInstance3D = model.get_node(bind.mesh_path)
				check(mesh.mesh.get_blend_shape_name(bind.index)==bind.name,"Blendshape binding mismatch")
				var affected := 0; var max_delta := 0.0
				for s in mesh.mesh.get_surface_count():
					var vertices: PackedVector3Array = mesh.mesh.surface_get_arrays(s)[Mesh.ARRAY_VERTEX]
					var morphs := mesh.mesh.surface_get_blend_shape_arrays(s)
					var morph: PackedVector3Array = morphs[bind.index][Mesh.ARRAY_VERTEX]
					for v in vertices.size():
						var delta := morph[v] if mesh.mesh.blend_shape_mode==Mesh.BLEND_SHAPE_MODE_RELATIVE else morph[v]-vertices[v]
						if delta.length()>0.000001: affected+=1; max_delta=maxf(max_delta,delta.length())
				check(affected>0,"Empty expression "+expression)
				mesh.set_blend_shape_value(bind.index,1.0)
				check(is_equal_approx(mesh.get_blend_shape_value(bind.index),1.0),"Expression write failed")
				mesh.set_blend_shape_value(bind.index,0.0)
				checked.append({"name":bind.name,"index":bind.index,"affected_vertices":affected,"max_delta_m":max_delta})
			record.expressions[expression]=checked
		results[id]=record
		print("VERIFIED ",id," ",JSON.stringify({"idle":record.clips.idle.right_hand_above_shoulder_m,"wave":record.clips.wave.right_hand_above_shoulder_m,"bone_count":sk.get_bone_count()}))
		model.free()
	results["failures"]=failures
	FileAccess.open("res://results/playback_verification.json",FileAccess.WRITE).store_string(JSON.stringify(results,"\t"))
	await process_frame
	quit(0 if failures.is_empty() else 1)
