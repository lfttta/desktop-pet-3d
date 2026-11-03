extends SceneTree

var failures: Array[String] = []
var checks := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures.append(label)

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var view: Node3D = load("res://scripts/character_view.gd").new()
	root.add_child(view)
	check(view.load_character({"config": "res://assets_data/godette.json"}), "Godette loads")
	await process_frame
	for method in ["request_action", "set_emotion", "play_test_audio", "pause_performance", "stop_audio", "reset_performance_state", "update_face"]:
		check(view.has_method(method), "has " + method)
	if failures.is_empty():
		for action in ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]:
			check(view.request_action(action) > 0, "accepts " + action)
		view.reset_performance_state()
		check(view.set_emotion("happy"), "happy available")
		view.blink_override = 0.8
		view.mouth_override = 0.6
		view.update_face()
		var face := view.model.get_node("Skeleton3D/Face") as MeshInstance3D
		check(face.get_blend_shape_value(3) > 0.7, "blink survives emotion")
		check(face.get_blend_shape_value(5) > 0.5, "mouth survives emotion")
		check(view.set_emotion("sad"), "sad replaces happy")
		view.update_face()
		check(is_zero_approx(face.get_blend_shape_value(9)), "previous happy mouth morph cleared")
		check(face.get_blend_shape_value(2) > 0.9, "sad preset applied")
		check(face.get_blend_shape_value(3) > 0.7 and face.get_blend_shape_value(5) > 0.5, "sad preserves blink and mouth")
		check(not view.set_emotion("missing"), "unknown emotion rejected")
		view.mouth_override = -1
		view.play_test_audio()
		await create_timer(0.3).timeout
		view.pause_performance(true)
		view.update_face()
		check(is_zero_approx(face.get_blend_shape_value(5)), "pause closes mouth")
		var paused_at: float = view.audio_player.get_playback_position()
		await create_timer(0.15).timeout
		check(absf(view.audio_player.get_playback_position() - paused_at) < 0.05, "pause freezes audio clock")
		view.pause_performance(false)
		view.stop_audio()
		view.update_face()
		check(is_zero_approx(face.get_blend_shape_value(5)), "stop closes mouth")
		view.reset_performance_state()
		check(view.action == "idle", "reset returns idle")
		check(view.emotion == "neutral", "reset returns neutral")
	view.free()
	# Allow the audio mixer to release its stopped playback before engine teardown.
	await create_timer(0.1).timeout
	print("GODETTE_PERFORMANCE checks=", checks, " failures=", failures.size())
	for failure in failures:
		push_error(failure)
	quit(0 if failures.is_empty() else 1)
