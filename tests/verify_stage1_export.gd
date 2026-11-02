extends SceneTree

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	quit(await verify_package(self))

static func packaged_path_exists(path: String) -> bool:
	return FileAccess.file_exists(path) or FileAccess.file_exists(path + ".remap") or FileAccess.file_exists(path + ".import") or ResourceLoader.exists(path)

static func verify_package(tree: SceneTree) -> int:
	var failures: Array[String] = []
	if OS.has_feature("editor") or not OS.has_feature("release"):
		failures.append("Package checks must run in the generated Release executable")
	print("STAGE1_EXPORT_BINARY ", OS.get_executable_path())
	for class_name_ in ["PetWindowBridge", "VRMSpringBoneSimulation"]:
		if not ClassDB.class_exists(class_name_):
			failures.append("Release DLL class is missing: " + class_name_)
	if ClassDB.class_exists("PetWindowBridge"):
		var bridge: RefCounted = ClassDB.instantiate("PetWindowBridge")
		for method in ["acquire_instance", "adopt_service_process", "terminate_service_processes", "get_displays_json"]:
			if not bridge.has_method(method):
				failures.append("Stage 1 native method is missing: " + method)
		bridge.shutdown()
	var list: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	if not list is Array or list.size() != 2:
		failures.append("Expected two bundled characters")
	for path in ["res://assets_data/avatar_sample_m.local.json", "res://models/AvatarSample_M.vrm", "res://tests/test_service_client.gd", "res://tests/pipe_probe.gd"]:
		if packaged_path_exists(path):
			failures.append("Development/local-only file included in package: " + path)
	for path in ["res://scripts/main.gd", "res://scripts/service_client.gd", "res://scripts/app_settings.gd", "res://scripts/ui/settings_window.gd", "res://scripts/ui/chat_window.gd"]:
		if not ResourceLoader.exists(path):
			failures.append("Stage 1 runtime resource missing: " + path)
	var character_script: Script = load("res://scripts/character_view.gd")
	if character_script == null:
		failures.append("Runtime character adapter missing from package")
	else:
		for id in ["godette", "swordsman"]:
			var view: Node3D = character_script.new()
			tree.root.add_child(view)
			if not view.load_character({"config": "res://assets_data/%s.json" % id}):
				failures.append("Packaged character failed to load: " + id)
			await tree.process_frame
			await tree.process_frame
			view.free()
	for path in ["res://scripts/ui/settings_window.gd", "res://scripts/ui/chat_window.gd"]:
		var window_script: Script = load(path)
		if window_script == null:
			continue
		var window: Window = window_script.new()
		window.visible = false
		tree.root.add_child(window)
		await tree.process_frame
		if window.transparent or window.transparent_bg:
			failures.append("Utility window is still transparent: " + path)
		if window.get_child_count() == 0 or not window.get_child(0) is ColorRect or (window.get_child(0) as ColorRect).color.a < 1.0:
			failures.append("Utility window has no opaque background: " + path)
		window.free()
	for failure in failures:
		push_error(failure)
	print("STAGE1_EXPORT_TEST failures=", failures.size())
	return 0 if failures.is_empty() else 1
