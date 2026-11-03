extends Node3D

const ACTIONS := ["idle", "walk", "turn", "wave", "pickup", "fall", "recover"]
const LABELS := ["待机", "行走", "转身", "挥手", "提起", "下落", "恢复"]
var actor: Node3D
var camera: Camera3D
var action_choice: OptionButton
var emotion_choice: OptionButton
var speed: SpinBox
var direction: CheckBox
var pause: CheckBox
var angle: HSlider
var audio_button: Button
var stop_button: Button
var elapsed := 0.0
var capture_path := ""
var exit_after := 0.0
var captured := false

func _ready() -> void:
	var window := get_window()
	window.title = "Godette · 动作与表情"
	window.borderless = false
	window.always_on_top = false
	window.unfocusable = false
	window.transparent = false
	window.transparent_bg = false
	window.size = Vector2i(960, 760)
	window.min_size = Vector2i(760, 600)
	window.position = Vector2i(120, 100)
	window.close_requested.connect(func():
		actor.reset_performance_state()
		get_tree().quit()
	)
	camera = Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = 3.1
	add_child(camera)
	_move_camera(0)
	camera.make_current()
	var environment := WorldEnvironment.new()
	environment.environment = Environment.new()
	environment.environment.background_mode = Environment.BG_COLOR
	environment.environment.background_color = Color(0.16, 0.18, 0.18)
	environment.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.environment.ambient_light_color = Color.WHITE
	environment.environment.ambient_light_energy = 0.4
	add_child(environment)
	var light := DirectionalLight3D.new()
	light.rotation_degrees = Vector3(-25, -30, 0)
	add_child(light)
	var layer := CanvasLayer.new()
	add_child(layer)
	var column := VBoxContainer.new()
	column.position = Vector2(16, 16)
	column.add_theme_constant_override("separation", 12)
	layer.add_child(column)
	var bar := HBoxContainer.new()
	bar.add_theme_constant_override("separation", 12)
	column.add_child(bar)
	_label(bar, "Godette")
	action_choice = OptionButton.new()
	for name_ in LABELS:
		action_choice.add_item(name_)
	bar.add_child(action_choice)
	action_choice.item_selected.connect(func(_index): _play())
	var replay := Button.new()
	replay.text = "播放"
	replay.pressed.connect(_play)
	bar.add_child(replay)
	pause = CheckBox.new()
	pause.text = "暂停"
	pause.toggled.connect(func(value): actor.pause_performance(value))
	bar.add_child(pause)
	_label(bar, "速度")
	speed = SpinBox.new()
	speed.min_value = 0.25
	speed.max_value = 2.0
	speed.step = 0.25
	speed.value = 1.0
	bar.add_child(speed)
	speed.value_changed.connect(func(_value): _play())
	direction = CheckBox.new()
	direction.text = "向左转"
	bar.add_child(direction)
	var second := HBoxContainer.new()
	second.add_theme_constant_override("separation", 12)
	column.add_child(second)
	_label(second, "表情")
	emotion_choice = OptionButton.new()
	for name_ in ["自然", "开心", "难过"]:
		emotion_choice.add_item(name_)
	second.add_child(emotion_choice)
	emotion_choice.item_selected.connect(func(index): actor.set_emotion(["neutral", "happy", "sad"][index]))
	audio_button = Button.new()
	audio_button.text = "播放声音"
	audio_button.tooltip_text = "本地测试音频"
	audio_button.pressed.connect(func(): actor.play_test_audio())
	second.add_child(audio_button)
	stop_button = Button.new()
	stop_button.text = "停止声音"
	stop_button.pressed.connect(func(): actor.stop_audio())
	second.add_child(stop_button)
	_label(second, "视角")
	angle = HSlider.new()
	angle.min_value = -180
	angle.max_value = 180
	angle.custom_minimum_size.x = 180
	angle.value_changed.connect(_move_camera)
	second.add_child(angle)
	actor = load("res://scripts/character_view.gd").new()
	add_child(actor)
	if not actor.load_character({"config": "res://assets_data/godette.json"}):
		get_tree().quit(1)
		return
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--capture="):
			capture_path = argument.trim_prefix("--capture=")
		if argument.begins_with("--auto-exit="):
			exit_after = float(argument.trim_prefix("--auto-exit="))
	_play()

func _label(parent: Control, text: String) -> void:
	var label := Label.new()
	label.text = text
	parent.add_child(label)

func _move_camera(degrees: float) -> void:
	var radians := deg_to_rad(degrees)
	camera.position = Vector3(sin(radians) * 4, 0.8, cos(radians) * 4)
	camera.look_at(Vector3(0, 0.8, 0))

func _play() -> void:
	if not is_instance_valid(actor):
		return
	pause.set_pressed_no_signal(false)
	actor.pause_performance(false)
	actor.action_controller.reset()
	actor.request_action(ACTIONS[action_choice.selected], "user", speed.value, -1 if direction.button_pressed else 1)

func _process(delta: float) -> void:
	elapsed += delta
	if not captured and not capture_path.is_empty() and elapsed > 0.6:
		captured = true
		await RenderingServer.frame_post_draw
		get_viewport().get_texture().get_image().save_png(capture_path)
	if exit_after > 0 and elapsed > exit_after:
		actor.reset_performance_state()
		get_tree().quit()
