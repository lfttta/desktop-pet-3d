extends SceneTree

var failures: Array[String] = []

func check(value: bool, message: String) -> void:
	if not value:
		failures.append(message)

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var preview: Node3D = load("res://scripts/ui/action_preview.gd").new()
	root.add_child(preview)
	await process_frame
	var view: Node3D = preview.actor
	view.set_process(false)
	view.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
	view.request_action("turn")
	view.player.seek(0.4, true)
	view.sync_turn_heading()
	var before: float = view.rotation.y
	view.request_action("turn")
	view.sync_turn_heading()
	check(absf(view.rotation.y - before) < 0.001, "repeating turn preserves heading at restart")
	check(view.player.current_animation_position < 0.001, "repeating turn restarts clip clock")
	preview.direction.button_pressed = true
	for action_index in [3, 4, 6]:
		preview.action_choice.select(action_index)
		preview._play()
		check(view.player.get_playing_speed() > 0, "left-turn option does not reverse " + preview.ACTIONS[action_index])
	view.set_emotion("happy")
	view.play_test_audio()
	view.stop_audio()
	view.update_face()
	var face := view.model.get_node("Skeleton3D/Face") as MeshInstance3D
	for index in [5, 6, 7, 8, 9]:
		check(is_zero_approx(face.get_blend_shape_value(index)), "stop resets vowel " + str(index))
	check(face.get_blend_shape_value(10) > 0.2, "mouth reset preserves non-mouth happy binding")
	view.set_process(true)
	view.play_test_audio()
	await create_timer(3.2).timeout
	view.update_face()
	for index in [5, 6, 7, 8, 9]:
		check(is_zero_approx(face.get_blend_shape_value(index)), "audio end resets vowel " + str(index))
	preview.free()
	await create_timer(0.1).timeout
	for failure in failures:
		push_error(failure)
	print("GODETTE_REVIEW_REGRESSIONS failures=", failures.size())
	quit(0 if failures.is_empty() else 1)
