extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func fixture(library_path: String) -> Dictionary:
	var model := (load("res://models/godette.vrm") as PackedScene).instantiate()
	root.add_child(model)
	for node in model.find_children("*", "AnimationPlayer", true, false):
		node.active = false
	for node in model.find_children("*", "SkeletonModifier3D", true, false):
		node.active = false
	var skeleton := model.get_node("Skeleton3D") as Skeleton3D
	var player := AnimationPlayer.new()
	model.add_child(player)
	player.root_node = NodePath("..")
	player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
	player.add_animation_library("test", load(library_path) as AnimationLibrary)
	return {"model": model, "skeleton": skeleton, "player": player}

func run() -> void:
	var baseline := fixture("res://motions/cache/godette.tres")
	var candidate := fixture("res://../build/stage2/godette.tres" if "--staged" in OS.get_cmdline_user_args() else "res://resources/animations/godette.tres")
	await process_frame
	for item in [baseline, candidate]:
		for controller in item.model.find_children("*", "Node3D", true, false):
			controller.set_process(false)
		for modifier in item.model.find_children("*", "SkeletonModifier3D", true, false):
			modifier.active = false
	var report := {}
	var failures := 0
	for action in ["idle", "wave"]:
		var animation_name: String = "test/" + action
		baseline.player.play(animation_name, 0)
		candidate.player.play(animation_name, 0)
		var duration: float = baseline.player.get_animation(animation_name).length
		var max_position := 0.0
		var max_rotation := 0.0
		var max_hip_height := 0.0
		var worst_bone := ""
		var frames := ceili(duration * 30.0) + 1
		for frame in frames:
			var time := minf(frame / 30.0, duration - 0.00001)
			baseline.player.seek(time, true)
			candidate.player.seek(time, true)
			baseline.skeleton.force_update_all_bone_transforms()
			candidate.skeleton.force_update_all_bone_transforms()
			var hip: int = baseline.skeleton.find_bone("Hips")
			var a_hip: Vector3 = baseline.skeleton.get_bone_global_pose(hip).origin
			var b_hip: Vector3 = candidate.skeleton.get_bone_global_pose(hip).origin
			max_hip_height = maxf(max_hip_height, absf(a_hip.y - b_hip.y))
			for bone in baseline.skeleton.get_bone_count():
				var a: Transform3D = baseline.skeleton.get_bone_global_pose(bone)
				var b: Transform3D = candidate.skeleton.get_bone_global_pose(bone)
				var error := a.origin.distance_to(b.origin)
				if error > max_position:
					max_position = error
					worst_bone = baseline.skeleton.get_bone_name(bone)
				var qa := a.basis.orthonormalized().get_rotation_quaternion().normalized()
				var qb := b.basis.orthonormalized().get_rotation_quaternion().normalized()
				max_rotation = maxf(max_rotation, 2.0 * acos(clampf(absf(qa.dot(qb)), 0, 1)))
		var passed := max_position < 0.001 and max_rotation < 0.002 and max_hip_height < 0.001
		if not passed:
			failures += 1
		report[action] = {"frames": frames, "bones": baseline.skeleton.get_bone_count(), "worst_bone": worst_bone, "max_position_error_m": max_position, "max_rotation_rad": max_rotation, "max_hip_height_error_m": max_hip_height, "pass": passed}
	baseline.model.free()
	candidate.model.free()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://../build/stage2/reports"))
	FileAccess.open("res://../build/stage2/reports/godette-regression.json", FileAccess.WRITE).store_string(JSON.stringify(report, "\t"))
	print("GODETTE_REGRESSION failures=", failures, " ", JSON.stringify(report))
	quit(0 if failures == 0 else 1)
