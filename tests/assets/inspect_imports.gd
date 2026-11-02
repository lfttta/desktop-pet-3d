extends SceneTree

const BASE := "D:/danjigame/3D-pet-gtpdo"
const OLD := "D:/danjigame/3D-pet/work-pet/prototypes/azpepoze/models/"

func _initialize() -> void:
	call_deferred("run")

func vec(v: Vector3) -> Array:
	return [v.x, v.y, v.z]

func transform_data(t: Transform3D) -> Dictionary:
	var q := t.basis.get_rotation_quaternion()
	return {"position":vec(t.origin), "rotation_xyzw":[q.x,q.y,q.z,q.w], "scale":vec(t.basis.get_scale())}

func load_model(path: String) -> Node3D:
	if path.ends_with(".vrm"):
		return load(BASE+"/tools/assets/runtime_vrm_loader.gd").load_vrm(path)
	var doc := GLTFDocument.new()
	var state := GLTFState.new()
	var err := doc.append_from_file(path, state, 8)
	assert(err == OK, "GLTF append failed: " + path)
	return doc.generate_scene(state)

func collect(n: Node, cls: String, out: Array) -> void:
	if n.is_class(cls): out.append(n)
	for child in n.get_children(): collect(child,cls,out)

func skin_rest_bounds(mi: MeshInstance3D) -> AABB:
	var box := AABB(); var first := true
	var sk: Skeleton3D = mi.get_node_or_null(mi.skeleton)
	var binds := []
	if mi.skin and sk:
		for i in mi.skin.get_bind_count():
			var b := sk.find_bone(mi.skin.get_bind_name(i))
			if b < 0: b=mi.skin.get_bind_bone(i)
			binds.append(mi.global_transform*sk.get_bone_global_rest(b)*mi.skin.get_bind_pose(i))
	for s in mi.mesh.get_surface_count():
		var a := mi.mesh.surface_get_arrays(s)
		var vertices: PackedVector3Array = a[Mesh.ARRAY_VERTEX]
		var weights = a[Mesh.ARRAY_WEIGHTS]; var bones = a[Mesh.ARRAY_BONES]
		var influences: int = weights.size()/vertices.size() if weights!=null and vertices.size()>0 else 0
		for v in vertices.size():
			var p: Vector3 = mi.global_transform*vertices[v]
			if influences>0 and not binds.is_empty():
				p=Vector3.ZERO
				for w in influences:
					var weight: float=weights[v*influences+w]
					if weight>0: p+=(binds[bones[v*influences+w]]*vertices[v])*weight
			if first: box=AABB(p,Vector3.ZERO); first=false
			else: box=box.expand(p)
	return box

func run() -> void:
	var inputs := {"idle":BASE+"/prototypes/stage0/motions/idle.glb", "wave":BASE+"/prototypes/stage0/motions/wave.glb", "godette":BASE+"/prototypes/stage0/models/godette.vrm", "swordsman":BASE+"/prototypes/stage0/models/swordsman.glb", "avatar_sample_m":OLD+"AvatarSample_M.vrm"}
	var results := {}
	for id in inputs:
		var model := load_model(inputs[id])
		assert(model != null, "Model import failed")
		root.add_child(model)
		var skels := []; collect(model,"Skeleton3D",skels)
		var meshes := []; collect(model,"MeshInstance3D",meshes)
		var players := []; collect(model,"AnimationPlayer",players)
		var record := {"skeletons":[],"meshes":[],"players":[]}
		for sk: Skeleton3D in skels:
			var bones := []
			for i in sk.get_bone_count():
				bones.append({"name":sk.get_bone_name(i),"parent":sk.get_bone_parent(i),"rest":transform_data(sk.get_bone_rest(i)),"global_rest":transform_data(sk.get_bone_global_rest(i)),"world_rest":transform_data(sk.global_transform*sk.get_bone_global_rest(i))})
			record.skeletons.append({"path":str(model.get_path_to(sk)),"transform":transform_data(sk.global_transform),"motion_scale":sk.motion_scale,"bones":bones})
		for mi: MeshInstance3D in meshes:
			var names := []
			for i in mi.mesh.get_blend_shape_count(): names.append(mi.mesh.get_blend_shape_name(i))
			var skin_box := skin_rest_bounds(mi)
			record.meshes.append({"path":str(model.get_path_to(mi)),"blendshapes":names,"aabb_position":vec((mi.global_transform*mi.get_aabb()).position),"aabb_size":vec((mi.global_transform*mi.get_aabb()).size),"skin_rest_position":vec(skin_box.position),"skin_rest_size":vec(skin_box.size)})
		for ap: AnimationPlayer in players:
			var anims := {}
			for name in ap.get_animation_list():
				var a := ap.get_animation(name)
				var tracks := []
				for t in a.get_track_count(): tracks.append({"path":str(a.track_get_path(t)),"type":a.track_get_type(t),"keys":a.track_get_key_count(t)})
				anims[name] = {"length":a.length,"tracks":tracks}
			record.players.append({"path":str(model.get_path_to(ap)),"root_node":str(ap.root_node),"animations":anims})
		results[id] = record
		print("INSPECTED ",id," skeletons=",skels.size()," meshes=",meshes.size()," players=",players.size())
		model.free()
	FileAccess.open("res://results/import_inspection.json",FileAccess.WRITE).store_string(JSON.stringify(results,"\t"))
	quit(0)


