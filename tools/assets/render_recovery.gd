extends SceneTree

const IDS := ["godette"]
const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]
var failures: Array[String] = []

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	root.size = Vector2i(480, 600)
	root.transparent_bg = false
	var world := Node3D.new()
	root.add_child(world)
	var camera := Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = 3.0
	camera.position = Vector3(0, 0.8, 4)
	world.add_child(camera)
	camera.look_at(Vector3(0, 0.8, 0))
	camera.make_current()
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-25, -30, 0)
	light.light_energy = 1.0
	world.add_child(light)
	var environment := WorldEnvironment.new()
	environment.environment = Environment.new()
	environment.environment.background_mode = Environment.BG_COLOR
	environment.environment.background_color = Color(0.16, 0.18, 0.18)
	environment.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.environment.ambient_light_color = Color.WHITE
	environment.environment.ambient_light_energy = 0.35
	world.add_child(environment)
	var title := Label.new()
	title.position = Vector2(12, 12)
	title.add_theme_font_size_override("font_size", 20)
	root.add_child(title)
	var directory := ProjectSettings.globalize_path("res://../build/stage2/reports/poses")
	DirAccess.make_dir_recursive_absolute(directory)
	var sheet := Image.create(1680, 300, false, Image.FORMAT_RGBA8)
	sheet.fill(Color(0.16, 0.18, 0.18))
	var report := {}
	for row in IDS.size():
		var id: String = IDS[row]
		var view: Node3D = load("res://scripts/character_view.gd").new()
		world.add_child(view)
		if not view.load_character({"config": "res://assets_data/%s.json" % id}):
			quit(1)
			return
		view.set_process(false)
		await process_frame
		for controller in view.spring_controllers:
			controller.set_process(false)
		view.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
		for modifier in view.model.find_children("*", "SkeletonModifier3D", true, false):
			modifier.active = false
		var facing: float = view.rotation.y
		for column in ACTIONS.size():
			var action: String = ACTIONS[column]
			var duration: float = view.player.get_animation("shared/" + action).length
			for side in [0, 1]:
				view.reset_performance_state()
				view.rotation.y = facing + side * PI * 0.5
				view.request_action(action)
				# Static samples omit crossfades; live crossfades have a separate test.
				view.player.stop()
				view.player.play("shared/" + action, 0)
				for sample in 5:
					var time := duration * minf(sample * 0.25, 0.9999)
					view.player.seek(time, true)
					view.sync_turn_heading()
					view.skeleton.force_update_all_bone_transforms()
					title.text = "%s / %s / %.2fs / %s" % [id, action, time, "front" if side == 0 else "side"]
					await process_frame
					await RenderingServer.frame_post_draw
					var frame := root.get_texture().get_image()
					var stem := "%s-%s-%s-%d" % [id, action, "front" if side == 0 else "side", sample]
					frame.save_png(directory.path_join(stem + ".png"))
					var box: Rect2 = view.project_visible_rect(camera, true)
					if not Rect2(2, 40, 476, 558).encloses(box):
						failures.append(stem + " bounds=" + str(box))
					if side == 0 and sample == 2:
						frame.convert(Image.FORMAT_RGBA8)
						frame.resize(240, 300)
						sheet.blit_rect(frame, Rect2i(0, 0, 240, 300), Vector2i(column * 240, row * 300))
						report[id + "/" + action] = view.describe()
		view.free()
		await process_frame
	sheet.save_png(directory.path_join("matrix.png"))
	FileAccess.open(directory.path_join("review.json"), FileAccess.WRITE).store_string(JSON.stringify({"poses": report, "framing_failures": failures}, "\t"))
	print("GODETTE_RENDER frames=70 framing_failures=", failures.size())
	for failure in failures:
		print(failure)
	quit(0 if failures.is_empty() else 1)
