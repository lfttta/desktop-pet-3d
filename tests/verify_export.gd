extends SceneTree

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	quit(await verify_package(self))

static func packaged_path_exists(path: String) -> bool:
	# Exported scripts/models often lose their raw source name but retain
	# remap/import metadata and a compiled/generated resource.
	return FileAccess.file_exists(path) or FileAccess.file_exists(path + ".remap") or FileAccess.file_exists(path + ".import") or ResourceLoader.exists(path)

static func verify_package(tree: SceneTree) -> int:
	var failures: Array[String] = []
	if OS.has_feature("editor") or not OS.has_feature("release"):
		failures.append("Package checks must run in the generated Release executable")
	print("EXPORT_BINARY ", OS.get_executable_path())
	for class_name_ in ["PetWindowBridge", "VRMSpringBoneSimulation"]:
		if not ClassDB.class_exists(class_name_):
			failures.append("Release DLL class is missing: " + class_name_)
	var list: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	if not list is Array or list.size() != 2:
		failures.append("Expected two bundled test characters")
	for path in ["res://assets_data/avatar_sample_m.local.json", "res://models/AvatarSample_M.vrm", "res://tests/test_interaction.gd"]:
		if packaged_path_exists(path):
			failures.append("Development/local-only file included in package: " + path)
	var script: Script = load("res://scripts/character_view.gd")
	if script == null:
		failures.append("Runtime adapter missing from package")
	else:
		for id in ["godette"]:
			var view: Node3D = script.new()
			tree.root.add_child(view)
			if not view.load_character({"config": "res://assets_data/%s.json" % id}):
				failures.append("Packaged character failed to load: " + id)
			await tree.process_frame
			await tree.process_frame
			view.free()
	for failure in failures:
		push_error(failure)
	print("EXPORT_TEST failures=", failures.size())
	return 0 if failures.is_empty() else 1
