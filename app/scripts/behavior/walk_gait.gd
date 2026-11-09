extends RefCounted
## Measure treadmill travel from the planted ankle, without seeking the live player.

const SAMPLES := 60

static func measure(skeleton: Skeleton3D, clip: Animation, forward: Vector3) -> float:
	if skeleton == null or clip == null or clip.length <= 0.0 or forward.length_squared() < 0.001:
		return 0.0
	var feet := [skeleton.find_bone("LeftFoot"), skeleton.find_bone("RightFoot")]
	if feet.has(-1):
		return 0.0
	var tracks := []
	for track in clip.get_track_count():
		var path := clip.track_get_path(track)
		if path.get_subname_count() != 1:
			continue
		var bone := skeleton.find_bone(String(path.get_subname(0)))
		if bone >= 0:
			tracks.append([track, bone, clip.track_get_type(track)])
	var samples: Array = [[], []]
	for step in SAMPLES + 1:
		var poses: Array[Transform3D] = []
		for bone in skeleton.get_bone_count():
			poses.append(skeleton.get_bone_rest(bone))
		var time := clip.length * float(step) / SAMPLES
		for entry in tracks:
			match int(entry[2]):
				Animation.TYPE_ROTATION_3D:
					poses[entry[1]].basis = Basis(clip.rotation_track_interpolate(entry[0], time))
				Animation.TYPE_POSITION_3D:
					poses[entry[1]].origin = clip.position_track_interpolate(entry[0], time) * skeleton.motion_scale
		for side in 2:
			var bone: int = feet[side]
			var pose := poses[bone]
			var parent := skeleton.get_bone_parent(bone)
			while parent >= 0:
				pose = poses[parent] * pose
				parent = skeleton.get_bone_parent(parent)
			samples[side].append(pose.origin)
	var rates: Array[float] = []
	var scale := skeleton.global_transform.basis.get_scale().abs()
	var axis := forward.normalized()
	for foot: Array in samples:
		var low := INF
		var high := -INF
		for point: Vector3 in foot:
			low = minf(low, point.y)
			high = maxf(high, point.y)
		var contact_height := low + (high - low) * 0.25
		for step in SAMPLES:
			var a: Vector3 = foot[step]
			var b: Vector3 = foot[step + 1]
			var travel := -((b - a) * scale).dot(axis) * SAMPLES
			if a.y <= contact_height and b.y <= contact_height and travel > 0.0:
				rates.append(travel)
	if rates.is_empty():
		return 0.0
	rates.sort()
	return rates[rates.size() / 2]
