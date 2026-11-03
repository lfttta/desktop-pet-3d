extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var app: Node3D = load("res://scripts/pet_window.gd").new()
	root.add_child(app)
	await process_frame
	var view: Node3D = app.actor
	var failures: Array[String] = []
	for iteration in 20:
		view.request_action("wave")
		view.set_emotion("happy")
		view.play_test_audio()
		app._handle_tray(1)
		if view.action != "idle" or view.audio_player.playing or view.emotion != "neutral":
			failures.append("hide did not reset state")
		await process_frame
		app._handle_tray(1)
		if not view.visible or view.action != "idle":
			failures.append("restore did not resume idle")
	view.request_action("turn")
	view.pause_performance(true)
	var position: float = view.player.current_animation_position
	await create_timer(0.15).timeout
	if view.player.current_animation_position != position:
		failures.append("pause advances body")
	view.pause_performance(false)
	view.reset_performance_state()
	app.free()
	await process_frame
	print("GODETTE_LIFECYCLE failures=", failures.size())
	for failure in failures:
		push_error(failure)
	quit(0 if failures.is_empty() else 1)
