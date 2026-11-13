extends RefCounted
## Cache CPU mesh data once. Bounds follow the current skeleton without GPU
## readback. Only indexed/rendered vertices participate; hidden props do not.
##
## The cached copy is compacted to the referenced vertices at configure time,
## so the permanent memory cost is the drawn surface, not the whole mesh, and
## the per-sample loop walks the smaller array.

var surfaces: Array = []
var cached := AABB()
var last_sample_usec := 0
var samples := 0
var total_sample_usec := 0
var _last_update := -1000000
var _sampling := false

func configure(meshes: Array[MeshInstance3D], skeleton: Skeleton3D) -> void:
	surfaces.clear()
	for mesh in meshes:
		if not mesh.visible or mesh.mesh == null:
			continue
		var binds: Array = []
		if mesh.skin:
			for i in mesh.skin.get_bind_count():
				var index := skeleton.find_bone(mesh.skin.get_bind_name(i))
				if index < 0:
					index = mesh.skin.get_bind_bone(i)
				binds.append({"bone": index, "rest": mesh.skin.get_bind_pose(i)})
		for surface in mesh.mesh.get_surface_count():
			var arrays := mesh.mesh.surface_get_arrays(surface)
			var vertices: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
			var weights: PackedFloat32Array = arrays[Mesh.ARRAY_WEIGHTS] if arrays[Mesh.ARRAY_WEIGHTS] != null else PackedFloat32Array()
			var bones: PackedInt32Array = arrays[Mesh.ARRAY_BONES] if arrays[Mesh.ARRAY_BONES] != null else PackedInt32Array()
			var used := PackedInt32Array()
			if arrays[Mesh.ARRAY_INDEX] != null:
				var unique := {}
				for index in arrays[Mesh.ARRAY_INDEX]:
					unique[index] = true
				used = PackedInt32Array(unique.keys())
			if used.is_empty():
				used = PackedInt32Array(range(vertices.size()))
			var influences := weights.size() / maxi(1, vertices.size())
			# Drop the unreferenced vertices (and their bone slots) for good: the
			# full arrays returned by surface_get_arrays are released here.
			var kept_vertices := PackedVector3Array()
			var kept_weights := PackedFloat32Array()
			var kept_bones := PackedInt32Array()
			kept_vertices.resize(used.size())
			if influences > 0:
				kept_weights.resize(used.size() * influences)
				kept_bones.resize(used.size() * influences)
			for j in used.size():
				var source := used[j]
				kept_vertices[j] = vertices[source]
				for slot in influences:
					kept_weights[j * influences + slot] = weights[source * influences + slot]
					kept_bones[j * influences + slot] = bones[source * influences + slot]
			surfaces.append({"mesh": mesh, "vertices": kept_vertices, "weights": kept_weights, "bones": kept_bones, "binds": binds, "influences": influences})
	_last_update = -1000000

func sample(skeleton: Skeleton3D, force := false, interval_usec := 100000) -> AABB:
	var now := Time.get_ticks_usec()
	if not is_instance_valid(skeleton) or not skeleton.is_inside_tree() or _sampling or (not force and now - _last_update < interval_usec):
		return cached
	_sampling = true
	_last_update = now
	var first := true
	var bounds := AABB()
	for surface: Dictionary in surfaces:
		var mesh: MeshInstance3D = surface.mesh
		# Skeleton modifiers can emit once while siblings are leaving the tree.
		if not is_instance_valid(mesh) or not mesh.is_inside_tree():
			continue
		var transforms: Array[Transform3D] = []
		for bind: Dictionary in surface.binds:
			transforms.append(mesh.global_transform * skeleton.get_bone_global_pose(bind.bone) * bind.rest)
		var vertices: PackedVector3Array = surface.vertices
		var bones: PackedInt32Array = surface.bones
		var weights: PackedFloat32Array = surface.weights
		var influences: int = surface.influences
		for index in vertices.size():
			var point: Vector3 = mesh.global_transform * vertices[index]
			if influences > 0 and not transforms.is_empty():
				point = Vector3.ZERO
				for slot in influences:
					var offset := index * influences + slot
					if weights[offset] > 0:
						point += (transforms[bones[offset]] * vertices[index]) * weights[offset]
			if first:
				bounds = AABB(point, Vector3.ZERO)
				first = false
			else:
				bounds = bounds.expand(point)
	if not first:
		cached = bounds
	last_sample_usec = Time.get_ticks_usec() - now
	total_sample_usec += last_sample_usec
	samples += 1
	_sampling = false
	return cached
