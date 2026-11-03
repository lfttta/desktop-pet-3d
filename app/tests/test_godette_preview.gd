extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var preview: Node3D = load("res://scripts/ui/action_preview.gd").new()
	root.add_child(preview)
	await process_frame
	var failures: Array[String] = []
	for index in 7:
		preview.action_choice.select(index)
		preview.action_choice.item_selected.emit(index)
		if preview.actor.action != preview.ACTIONS[index]:
			failures.append("wrong action")
	preview.emotion_choice.select(1)
	preview.emotion_choice.item_selected.emit(1)
	if preview.actor.emotion != "happy":
		failures.append("emotion selector")
	preview.audio_button.pressed.emit()
	await create_timer(0.2).timeout
	preview.pause.button_pressed = true
	if not preview.actor.audio_player.stream_paused:
		failures.append("audio pause")
	preview.pause.button_pressed = false
	preview.stop_button.pressed.emit()
	if preview.actor.audio_player.playing:
		failures.append("audio stop")
	preview.angle.value = 90
	preview.angle.value = 0
	preview.action_choice.select(0)
	preview.action_choice.item_selected.emit(0)
	preview.actor.blink_override = 0
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://../build/stage2/reports"))
	for index in 3:
		preview.emotion_choice.select(index)
		preview.emotion_choice.item_selected.emit(index)
		await create_timer(0.1).timeout
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png(ProjectSettings.globalize_path("res://../build/stage2/reports/emotion-%d.png" % index))
	preview.actor.play_test_audio()
	var max_mouth := 0.0
	var min_mouth := 1.0
	var face := preview.actor.model.get_node("Skeleton3D/Face") as MeshInstance3D
	for frame in 70:
		await create_timer(0.05).timeout
		var mouth := face.get_blend_shape_value(5)
		max_mouth = maxf(max_mouth, mouth)
		min_mouth = minf(min_mouth, mouth)
	if max_mouth < 0.5 or min_mouth > 0.05 or preview.actor.audio_player.playing or face.get_blend_shape_value(5) > 0.01:
		failures.append("audio-clock mouth envelope/end reset")
	preview.free()
	await process_frame
	print("GODETTE_PREVIEW failures=", failures.size(), " mouth_range=", min_mouth, ",", max_mouth)
	for failure in failures:
		push_error(failure)
	quit(0 if failures.is_empty() else 1)
