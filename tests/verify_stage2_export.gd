extends SceneTree

const IDS := ["godette"]
const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	quit(await verify_package(self))

static func packaged_path_exists(path: String) -> bool:
	return FileAccess.file_exists(path) or FileAccess.file_exists(path + ".remap") or ResourceLoader.exists(path)

static func verify_package(tree: SceneTree) -> int:
	var failures: Array[String] = []
	var catalog: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	if not catalog is Array or catalog.size() != 1:
		failures.append("Expected only Godette")
	else:
		var ids: Array = catalog.map(func(entry): return entry.id)
		if ids != IDS:
			failures.append("Stage 2 catalog IDs are wrong")
	for forbidden in ["res://assets_data/swordsman.json", "res://models/swordsman.glb", "res://models/cartoon_skeleton.glb", "res://models/man.glb", "res://models/candidate_avatar_sample_m.vrm", "res://models/alicia.vrm", "res://models/soldier.glb", "res://resources/animations/alicia.tres", "res://resources/animations/soldier.tres"]:
		if packaged_path_exists(forbidden):
			failures.append("Excluded swordsman asset is packaged: " + forbidden)
	var script: Script = load("res://scripts/character_view.gd")
	for character_id in IDS:
		var view: Node3D = script.new()
		tree.root.add_child(view)
		if not view.load_character({"config": "res://assets_data/%s.json" % character_id}):
			failures.append("Character failed package load: " + character_id)
		else:
			await tree.process_frame
			for action in ACTIONS:
				if view.request_action(action, "user") < 0:
					failures.append(character_id + " cannot play " + action)
		view.free()
		await tree.process_frame
	for failure in failures:
		push_error(failure)
	print("STAGE2_EXPORT_TEST failures=", failures.size())
	return 0 if failures.is_empty() else 1
