extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var review: Node3D = load("res://scripts/review.gd").new()
	root.add_child(review)
	await process_frame
	var failures: Array[String] = []
	for index in 3:
		review.character_choice.select(index)
		review.character_choice.item_selected.emit(index)
		await process_frame
		if review.actor.config.id != review.IDS[index]:
			failures.append("character selection " + str(index))
		for action_index in 7:
			review.action_choice.select(action_index)
			review.action_choice.item_selected.emit(action_index)
			if review.actor.action != review.ACTIONS[action_index]:
				failures.append("action selection " + str(action_index))
		review.pause.button_pressed = true
		var paused_at: float = review.actor.player.current_animation_position
		await create_timer(0.15).timeout
		if review.actor.player.current_animation_position != paused_at:
			failures.append("pause advances animation")
		review.pause.button_pressed = false
		review.speed.value = 1.5
		review.angle.value = 90
		review.angle.value = 0
		review.speed.value = 1.0
	review.character_choice.select(0)
	review.character_choice.item_selected.emit(0)
	review.action_choice.select(0)
	review.action_choice.item_selected.emit(0)
	await create_timer(0.3).timeout
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(ProjectSettings.globalize_path("res://../reports/review-window.png"))
	print("RECOVERY_UI failures=", failures.size())
	for failure in failures:
		push_error(failure)
	review.free()
	await process_frame
	quit(0 if failures.is_empty() else 1)
