extends SceneTree
## Asset-line gate for one humanoid_core contract: the staged bake must carry
## every manifest action, mapped bones must stay sane, and the pose matrix must
## render actual geometry. Run as:
##   godot --path app --script tools/assets/verify_character_assets.gd -- --character=<id>

const MANIFEST_PATH := "res://../animations/humanoid_core/manifest.json"
const CONTRACT_PATH := "res://assets_data/%s.json"
const STAGED_LIBRARY := "res://../build/stage2/%s.tres"
const BACKGROUND := Color(0.16, 0.18, 0.18)

var failures: Array[String] = []
var checks := 0

func check(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures.append(message)

func _initialize() -> void:
	call_deferred("run")

func user_argument(prefix: String) -> String:
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with(prefix):
			return argument.trim_prefix(prefix)
	return ""

## A frame counts as non-empty when the rendered body area differs from the flat
## backdrop. The top strip carries the caption and is excluded on purpose.
func frame_has_content(frame: Image) -> bool:
	if frame.get_height() <= 60:
		return false
	var body := frame.get_region(Rect2i(0, 60, frame.get_width(), frame.get_height() - 60))
	body.resize(60, 60, Image.INTERPOLATE_BILINEAR)
	body.convert(Image.FORMAT_RGBA8)
	for y in body.get_height():
		for x in body.get_width():
			var pixel := body.get_pixel(x, y)
			if absf(pixel.r - BACKGROUND.r) > 0.02 or absf(pixel.g - BACKGROUND.g) > 0.02 or absf(pixel.b - BACKGROUND.b) > 0.02:
				return true
	return false

func run() -> void:
	var character := user_argument("--character=")
	if character.is_empty():
		push_error("usage: -- --character=<id>")
		quit(2)
		return
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(CONTRACT_PATH % character))
	if not parsed is Dictionary:
		push_error("Missing runtime contract: " + (CONTRACT_PATH % character))
		quit(2)
		return
	var config: Dictionary = parsed
	var manifest: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(MANIFEST_PATH))
	var actions: Array = manifest.actions.keys()
	var library := load(STAGED_LIBRARY % character) as AnimationLibrary
	if library == null:
		push_error("Missing staged animation library: " + (STAGED_LIBRARY % character))
		quit(2)
		return
	for action in actions:
		check(library.has_animation(str(action)), "%s/%s missing from staged library" % [character, action])

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
	environment.environment.background_color = BACKGROUND
	environment.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.environment.ambient_light_color = Color.WHITE
	environment.environment.ambient_light_energy = 0.35
	world.add_child(environment)
	var title := Label.new()
	title.position = Vector2(12, 12)
	title.add_theme_font_size_override("font_size", 20)
	root.add_child(title)

	var packed := load(str(config.model_path)) as PackedScene
	if packed == null:
		push_error("Cannot load model: " + str(config.model_path))
		quit(2)
		return
	var model := packed.instantiate() as Node3D
	world.add_child(model)
	await process_frame
	var skeleton := model.get_node_or_null(str(config.skeleton_path)) as Skeleton3D
	if skeleton == null:
		push_error("%s: cannot resolve skeleton %s" % [character, config.skeleton_path])
		quit(3)
		return
	for node in model.find_children("*", "AnimationPlayer", true, false):
		node.stop()
		node.active = false
	for modifier in model.find_children("*", "SkeletonModifier3D", true, false):
		modifier.active = false
	# Same display normalization the runtime adapter applies from the contract.
	var display: Dictionary = config.get("display", {})
	var minimum: Array = display.get("rest_bounds_min", [0, 0, 0])
	var maximum: Array = display.get("rest_bounds_max", [0, 1.6, 0])
	var factor := 1.6 / maxf(0.1, float(maximum[1]) - float(minimum[1]))
	model.scale = Vector3.ONE * factor
	model.position.y = float(display.get("ground_offset_y", 0)) * factor
	var angles: Array = display.get("rotation_degrees", [0, 0, 0])
	model.rotation = Vector3(deg_to_rad(float(angles[0])), deg_to_rad(float(angles[1])), deg_to_rad(float(angles[2])))
	var facing := model.rotation.y

	var player := AnimationPlayer.new()
	player.name = "StagedActions"
	model.add_child(player)
	player.root_node = NodePath("..")
	player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL
	player.add_animation_library("shared", library)

	var bone_map: Dictionary = config.get("bone_map", {})
	var mapped: Dictionary = {}
	for profile in bone_map:
		var index := skeleton.find_bone(str(bone_map[profile]))
		if index >= 0:
			mapped[profile] = index
	check(mapped.has("Hips"), character + ": Hips mapping missing")
	var hips: int = mapped.get("Hips", -1)
	# The band is derived from this skeleton, so it holds for any model scale.
	# The lower edge accommodates floor-bound clips (laying, sleeping, sitting)
	# while still catching a collapsed or inverted rig.
	var rest_hip := (skeleton.global_transform * skeleton.get_bone_global_rest(hips)).origin.y if hips >= 0 else 0.0
	var lower_band := rest_hip * 0.05
	var upper_band := rest_hip * 3.0
	check(rest_hip > 0.05, character + ": rest hip height is implausible")

	var directory := ProjectSettings.globalize_path("res://../build/stage2/reports/%s-poses" % character)
	DirAccess.make_dir_recursive_absolute(directory)
	var matrix := Image.create(actions.size() * 240, 300, false, Image.FORMAT_RGBA8)
	matrix.fill(BACKGROUND)
	var report := {}
	var frames := 0
	for column in actions.size():
		var action: String = str(actions[column])
		if not library.has_animation(action):
			continue
		var duration: float = library.get_animation(action).length
		player.stop()
		player.play("shared/" + action, 0)
		var samples := ceili(duration * 30.0) + 1
		var min_hip := INF
		var max_hip := -INF
		for sample in samples:
			player.seek(minf(sample / 30.0, duration - 0.00001), true)
			skeleton.force_update_all_bone_transforms()
			for profile in mapped:
				var pose: Transform3D = skeleton.get_bone_global_pose(int(mapped[profile]))
				check(pose.is_finite(), "%s/%s finite %s" % [character, action, profile])
			if hips >= 0:
				var hip_height: float = (skeleton.global_transform * skeleton.get_bone_global_pose(hips)).origin.y
				min_hip = minf(min_hip, hip_height)
				max_hip = maxf(max_hip, hip_height)
				check(is_finite(hip_height) and hip_height >= lower_band and hip_height <= upper_band,
					"%s/%s hip height %.3f outside [%.3f, %.3f]" % [character, action, hip_height, lower_band, upper_band])
		# Front and side views at five evenly spaced instants.
		for side in [0, 1]:
			model.rotation.y = facing + side * PI * 0.5
			for sample in 5:
				var time := duration * minf(sample * 0.25, 0.9999)
				player.seek(time, true)
				skeleton.force_update_all_bone_transforms()
				title.text = "%s / %s / %.2fs / %s" % [character, action, time, "front" if side == 0 else "side"]
				await process_frame
				await RenderingServer.frame_post_draw
				var frame := root.get_texture().get_image()
				var stem := "%s-%s-%s-%d" % [character, action, "front" if side == 0 else "side", sample]
				frame.save_png(directory.path_join(stem + ".png"))
				check(frame_has_content(frame), stem + " rendered no geometry")
				frames += 1
				if side == 0 and sample == 2:
					var thumbnail := frame.duplicate()
					thumbnail.resize(240, 300, Image.INTERPOLATE_BILINEAR)
					thumbnail.convert(Image.FORMAT_RGBA8)
					matrix.blit_rect(thumbnail, Rect2i(0, 0, 240, 300), Vector2i(column * 240, 0))
		model.rotation.y = facing
		report[action] = {"frames": samples, "min_hip_height_m": min_hip, "max_hip_height_m": max_hip}
	matrix.save_png(directory.path_join("matrix.png"))
	FileAccess.open(directory.path_join("review.json"), FileAccess.WRITE).store_string(JSON.stringify({"checks": checks, "failures": failures, "actions": report}, "\t"))
	print("CHARACTER_ASSETS character=", character, " frames=", frames, " failures=", failures.size())
	for failure in failures.slice(0, 20):
		print(failure)
	quit(0 if failures.is_empty() else 1)
