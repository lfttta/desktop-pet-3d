extends SceneTree

const IDS := ["godette"]
const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]
const CharacterView = preload("res://scripts/character_view.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("run")


func pose_is_reasonable(view: Node3D) -> bool:
	var details: Dictionary = view.describe()
	var positions: Dictionary = details.get("positions", {})
	if not positions.has("Hips"):
		return false
	var hips_value: Array = positions.Hips
	var hips := Vector3(float(hips_value[0]), float(hips_value[1]), float(hips_value[2]))
	var head_value: Array = positions.get("Head", [])
	if head_value.size() != 3:
		return false
	var head := Vector3(float(head_value[0]), float(head_value[1]), float(head_value[2]))
	if head.distance_to(hips) < 0.2:
		return false
	for value: Array in positions.values():
		var point := Vector3(float(value[0]), float(value[1]), float(value[2]))
		if not point.is_finite() or point.distance_to(hips) > 4.0:
			return false
	return true


func run() -> void:
	for character_id in IDS:
		var view := CharacterView.new()
		root.add_child(view)
		await process_frame
		check(view.load_character({"config": "res://assets_data/%s.json" % character_id}), character_id + " loads")
		await process_frame
		for action in ACTIONS:
			var request: int = view.request_action(action, "user")
			check(request > 0, character_id + " requests " + action)
			check(view.action == action, character_id + " enters " + action)
			view.player.seek(view.player.current_animation_length * 0.5, true)
			view.skeleton.force_update_all_bone_transforms()
			var bounded_pose := pose_is_reasonable(view)
			if not bounded_pose:
				print("UNBOUNDED_POSE ", character_id, " ", action, " ", JSON.stringify(view.describe()))
			check(bounded_pose, character_id + " keeps " + action + " within a plausible humanoid span")
		var safe_box: AABB = view.action_safe_bounds("wave")
		check(safe_box.size.x > 0.0 and safe_box.size.y > 0.0, character_id + " exposes action-safe bounds")
		check(view.foot_anchor().is_finite(), character_id + " exposes a finite foot anchor")
		view.reset_performance_state()
		check(view.action == "idle", character_id + " reset returns idle")
		view.free()
		await process_frame
	print("STAGE2_CHARACTER_VIEW_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
