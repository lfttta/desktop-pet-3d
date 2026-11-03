extends SceneTree

const IDS := ["godette"]
const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	for character_id in IDS:
		var config_path := "res://assets_data/%s.json" % character_id
		var config: Variant = JSON.parse_string(FileAccess.get_file_as_string(config_path))
		check(config is Dictionary, character_id + " config parses")
		if not config is Dictionary:
			continue
		var packed := load(str(config.model_path)) as PackedScene
		check(packed != null, character_id + " model loads")
		if packed:
			var model := packed.instantiate()
			root.add_child(model)
			var skeleton := model.get_node_or_null(str(config.skeleton_path)) as Skeleton3D
			check(skeleton != null, character_id + " skeleton path resolves")
			if skeleton:
				for required in ["Hips", "Spine", "Head", "LeftUpperArm", "RightUpperArm", "LeftUpperLeg", "RightUpperLeg", "LeftFoot", "RightFoot"]:
					check(skeleton.find_bone(str(config.bone_map.get(required, ""))) >= 0, character_id + " maps " + required)
			model.free()
		var library := load(str(config.animation_library)) as AnimationLibrary
		check(library != null, character_id + " animation library loads")
		if library:
			for action in ACTIONS:
				check(library.has_animation(action), character_id + " has action " + action)
				if library.has_animation(action):
					check(library.get_animation(action).length > 0.0, character_id + " " + action + " has duration")
	print("STAGE2_LIBRARY_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
