extends SceneTree

var failures: Array[String] = []
var checks := 0

func check(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures.append(message)

func _initialize() -> void:
	call_deferred("run")

func disable_secondary(view: Node3D) -> void:
	view.set_process(false)
	for controller in view.spring_controllers:
		controller.set_process(false)
	for modifier in view.model.find_children("*", "SkeletonModifier3D", true, false):
		modifier.active = false

func point(view: Node3D, name_: String) -> Vector3:
	return view.skeleton.global_transform * view.skeleton.get_bone_global_pose(view.skeleton.find_bone(view.bone_map[name_])).origin

func run() -> void:
	var report := {}
	var profile := SkeletonProfileHumanoid.new()
	var standard_names: Array[String] = []
	for index in profile.bone_size:
		standard_names.append(profile.get_bone_name(index))
	var ids: Array = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	for entry in ids:
		var view: Node3D = load("res://scripts/character_view.gd").new()
		root.add_child(view)
		check(view.load_character(entry), entry.id + " loads")
		await process_frame
		disable_secondary(view)
		view.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
		var mapped: Array = view.bone_map.values()
		for name_ in view.bone_map:
			check(name_ in standard_names, entry.id + " uses humanoid name " + str(name_))
			check(mapped.count(view.bone_map[name_]) == 1, entry.id + " has unique mapping " + str(name_))
		var rests: Array[float] = []
		for bone in view.skeleton.get_bone_count():
			rests.append(view.skeleton.get_bone_rest(bone).origin.length())
		var character := {}
		for action in ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]:
			view.reset_performance_state()
			check(view.request_action(action) > 0, entry.id + " accepts " + action)
			view.player.stop()
			view.player.play("shared/" + action, 0)
			var duration: float = view.player.get_animation("shared/" + action).length
			var max_length_error := 0.0
			var min_hip_above_foot := INF
			var samples := ceili(duration * 60) + 1
			for sample in samples:
				view.player.seek(minf(sample / 60.0, duration - 0.00001), true)
				view.skeleton.force_update_all_bone_transforms()
				for bone in view.skeleton.get_bone_count():
					var name_: String = view.skeleton.get_bone_name(bone)
					var pose: Transform3D = view.skeleton.get_bone_pose(bone)
					check(pose.is_finite(), entry.id + "/" + action + " finite " + name_)
					if name_ != view.bone_map.Hips:
						max_length_error = maxf(max_length_error, absf(pose.origin.length() - rests[bone]))
				var foot := maxf(point(view, "LeftFoot").y, point(view, "RightFoot").y)
				min_hip_above_foot = minf(min_hip_above_foot, point(view, "Hips").y - foot)
			check(max_length_error < 0.001, entry.id + "/" + action + " preserves bone lengths")
			if action != "fall":
				check(min_hip_above_foot > 0.2, entry.id + "/" + action + " keeps legs below hips")
			character[action] = {"frames": samples, "max_bone_length_error_m": max_length_error, "min_hip_above_foot_m": min_hip_above_foot}
		view.reset_performance_state()
		var start: float = view.rotation.y
		view.request_action("turn", "user", 1.0, -1)
		for tick in 70:
			view.player.advance(1.0 / 60.0)
			view.sync_turn_heading()
		check(absf(view.rotation.y - start + PI) < 0.001, entry.id + " turns exactly once to the left")
		check(view.action == "idle", entry.id + " returns idle after turn")
		var held: int = view.request_action("pickup", "drag")
		view.player.advance(1.0)
		check(view.action == "pickup", entry.id + " holds suspended pose")
		check(view.request_action("wave", "chat") < 0, entry.id + " prevents low-priority interruption")
		view.release_action(held)
		check(view.action == "idle", entry.id + " releases held pose")
		var old: int = view.request_action("wave")
		view.request_action("fall", "fall")
		view.action_controller.complete(old)
		check(view.action == "fall", entry.id + " ignores stale completion")
		for action in ["idle", "wave", "pickup", "fall", "recover", "walk", "idle"]:
			view.reset_performance_state()
			view.request_action(action)
			for tick in 12:
				view.player.advance(1.0 / 60.0)
				view.skeleton.force_update_all_bone_transforms()
				check(point(view, "Head").is_finite(), entry.id + " crossfade finite")
		view.free()
		await process_frame
		report[entry.id] = character
	var baseline_nodes := int(Performance.get_monitor(Performance.OBJECT_NODE_COUNT))
	var warmed_memory := 0.0
	var checkpoints := []
	for iteration in 30:
		var view: Node3D = load("res://scripts/character_view.gd").new()
		root.add_child(view)
		check(view.load_character(ids[iteration % ids.size()]), "switch loads")
		await process_frame
		view.request_action("wave")
		await process_frame
		view.reset_performance_state()
		view.free()
		await process_frame
		await process_frame
		check(int(Performance.get_monitor(Performance.OBJECT_NODE_COUNT)) == baseline_nodes, "switch releases all scene nodes")
		if iteration == 9:
			warmed_memory = Performance.get_monitor(Performance.MEMORY_STATIC)
		if iteration in [9, 19, 29]:
			checkpoints.append({"switches": iteration + 1, "nodes": Performance.get_monitor(Performance.OBJECT_NODE_COUNT), "static_bytes": Performance.get_monitor(Performance.MEMORY_STATIC)})
	var growth := Performance.get_monitor(Performance.MEMORY_STATIC) - warmed_memory
	check(growth < 262144, "30 switches grow less than 256 KiB after warmup")
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://../reports"))
	FileAccess.open("res://../reports/runtime-validation.json", FileAccess.WRITE).store_string(JSON.stringify({"checks": checks, "failures": failures, "characters": report, "switching": checkpoints, "growth_bytes": growth}, "\t"))
	print("RECOVERY_RUNTIME checks=", checks, " failures=", failures.size())
	for failure in failures.slice(0, 20):
		push_error(failure)
	quit(0 if failures.is_empty() else 1)
