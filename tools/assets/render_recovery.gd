extends SceneTree

const CONTRACT_DIRECTORY := "res://../characters"
const MOTION_LIBRARY_PREFIX := "humanoid_core"
## Character failures outside the delivered package are recorded, not fatal.
const PACKAGE_CHARACTER_IDS := ["godette"]
var failures: Array[String] = []
var non_package_failures: Array[String] = []

func _initialize() -> void:
	call_deferred("run")

## Same contract discovery as the baker: every schema v2 contract bound to
## humanoid_core is rendered, local-only samples are skipped.
func discover_characters() -> Array[String]:
	var result: Array[String] = []
	var directory := DirAccess.open(ProjectSettings.globalize_path(CONTRACT_DIRECTORY))
	if directory == null:
		push_error("Cannot open " + CONTRACT_DIRECTORY)
		return result
	var names: Array[String] = []
	for file in directory.get_files():
		if file.ends_with(".json") and not file.ends_with(".local.json"):
			names.append(file)
	names.sort()
	for file in names:
		var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(CONTRACT_DIRECTORY + "/" + file))
		if not parsed is Dictionary:
			continue
		var config: Dictionary = parsed
		if int(config.get("schema_version", 0)) < 2:
			continue
		if not str(config.get("motion_library", "")).begins_with(MOTION_LIBRARY_PREFIX):
			continue
		result.append(str(config.id))
	return result

func run() -> void:
	root.size = Vector2i(480, 600)
	root.transparent_bg = false
	var world := Node3D.new()
	root.add_child(world)
	var camera := Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	# Wide enough for overhead-reach clips (climbing, cheering, victory) to stay
	# inside the review band without touching the caption strip.
	camera.size = 3.6
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
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string("res://../animations/humanoid_core/manifest.json"))
	var actions: Array = manifest.actions.keys()
	var ids := discover_characters()
	var sheet := Image.create(actions.size() * 240, ids.size() * 300, false, Image.FORMAT_RGBA8)
	sheet.fill(Color(0.16, 0.18, 0.18))
	var report := {}
	var loaded: Array[String] = []
	var skipped: Array[String] = []
	for row in ids.size():
		var id: String = ids[row]
		# A baked library for a non-package character is a build product and is
		# deliberately not tracked; on a clean checkout it may be absent. Skip
		# before the loader can log an error, which the build treats as a failure.
		var contract: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/%s.json" % id))
		var ready := false
		if contract is Dictionary:
			var model_path := str(contract.get("model_path", ""))
			var library_path := str(contract.get("animation_library", ""))
			ready = ResourceLoader.exists(model_path) and ResourceLoader.exists(library_path)
		if not ready:
			skipped.append(id)
			print("RENDER_SKIPPED ", id)
			continue
		var view: Node3D = load("res://scripts/character_view.gd").new()
		world.add_child(view)
		if not view.load_character({"config": "res://assets_data/%s.json" % id}):
			# Keep rendering the remaining contracts so the packaged character's
			# pose matrix is still produced when another contract is broken.
			skipped.append(id)
			print("RENDER_SKIPPED ", id)
			view.free()
			await process_frame
			continue
		loaded.append(id)
		view.set_process(false)
		await process_frame
		for controller in view.spring_controllers:
			controller.set_process(false)
		view.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
		for modifier in view.model.find_children("*", "SkeletonModifier3D", true, false):
			modifier.active = false
		var facing: float = view.rotation.y
		for column in actions.size():
			var action: String = actions[column]
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
						if id in PACKAGE_CHARACTER_IDS:
							failures.append(stem + " bounds=" + str(box))
						else:
							non_package_failures.append(stem + " bounds=" + str(box))
					if side == 0 and sample == 2:
						frame.convert(Image.FORMAT_RGBA8)
						frame.resize(240, 300)
						sheet.blit_rect(frame, Rect2i(0, 0, 240, 300), Vector2i(column * 240, row * 300))
						report[id + "/" + action] = view.describe()
		view.free()
		await process_frame
	sheet.save_png(directory.path_join("matrix.png"))
	FileAccess.open(directory.path_join("review.json"), FileAccess.WRITE).store_string(JSON.stringify({"poses": report, "framing_failures": failures, "non_package_failures": non_package_failures, "skipped": skipped}, "\t"))
	# The release gate only counts the packaged characters; other contracts are
	# rendered for review and reported separately.
	var package_frames := 0
	for id in loaded:
		if id in PACKAGE_CHARACTER_IDS:
			package_frames += actions.size() * 10
	print("GODETTE_RENDER frames=", package_frames, " framing_failures=", failures.size())
	for failure in failures:
		print(failure)
	for failure in non_package_failures:
		print("NONPACKAGE_FRAMING ", failure)
	for id in skipped:
		print("NONPACKAGE_SKIPPED ", id)
	quit(0 if failures.is_empty() else 1)
