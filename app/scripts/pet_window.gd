extends Node3D
## Stage 0 application: rendering/interaction and native lifetime only.

## Emitted when the visible speech bubble was clicked. The app layer opens chat.
signal bubble_clicked

const DragState = preload("res://scripts/drag_state.gd")
const HitRegions = preload("res://scripts/hit_regions.gd")
const ScreenMath = preload("res://scripts/screen_math.gd")
const ActionMenu = preload("res://scripts/action_menu.gd")
const MotionController = preload("res://scripts/motion/motion_controller.gd")
const BehaviorController = preload("res://scripts/behavior/behavior_controller.gd")
const SpeechBubble = preload("res://scripts/ui/speech_bubble.gd")
## Pixels between the top of the head and the bottom of the bubble.
const BUBBLE_GAP := 10.0
## Slack kept on each side when the bubble is limited to the window width.
const BUBBLE_WINDOW_MARGIN := 12.0
## Longest reply a bubble shows before an ellipsis. The chat window keeps the
## full text; only the desktop bubble is clipped so it does not cover the pet.
const REPLY_BUBBLE_CHARS := 40
const CLICK_PHRASE_CATEGORIES := ["greeting", "care"]
## The start greeting is a hello only; a care reminder would be a strange first
## line after launching the pet.
const START_PHRASE_CATEGORIES := ["greeting"]
## Wall-clock windows for phrase selection, copied from TIME_WINDOWS in
## service/expression/phrases.py. Each range is half-open and `night` wraps
## midnight; `any` stays valid at every hour.
const TIME_WINDOWS := {
	"morning": Vector2i(5, 11),
	"afternoon": Vector2i(11, 17),
	"evening": Vector2i(17, 22),
	"night": Vector2i(22, 5),
}
const TIME_WINDOW_ANY := "any"

var camera: Camera3D
var actor: Node3D
var bridge: RefCounted
var drag := DragState.new()
var options := {}
var regions: Array = []
var elapsed := 0.0
var frame_samples: Array[float] = []
var hidden := false
var captured_image := false
var quitting := false
var current_action := "idle"
var current_character := 0
var characters: Array = []
var screen := 0
var dpi_scale := 1.0
var previous_interactive := false
var status_label: Label
## Created on first use so a headless run that never speaks pays nothing.
var speech_bubble: Control
var _bubble_layer: CanvasLayer
## Mirrors the service's bubble.enabled setting: false makes show_bubble a no-op.
var _bubble_enabled := true
## Mirrors bubble.duration_seconds; -1 keeps the per-text default. Named
## `duration` because main.gd's `_bubble_seconds()` helper shadows a field of
## that name in the derived class.
var _bubble_duration := -1.0
## Mirrors the service's greeting group (service/settings.py GREETING_DEFAULTS):
## whether greetings are spoken, whether one is said after startup, and how many
## minutes a click phrase keeps the next one from replacing it.
var greeting := {"enabled": true, "on_start": true, "cooldown_minutes": 60}
## Injectable wall clock for the time window and the click cooldown. It returns
## a Dictionary with `hour` (local wall clock, 0-23) and `minutes` (fractional
## minutes on one timeline). Left invalid, the local system clock is used; a
## test swaps it in so it can pin an hour and advance time without waiting.
var greeting_clock: Callable = Callable()
## Timeline minutes of the last click phrase and whether one has been said yet,
## so the first click is never held back by the cooldown.
var _click_greeting_said := false
var _click_greeting_minutes := 0.0
## True while a press began on the bubble, so releasing it opens chat instead of
## counting as a click on the pet.
var _press_on_bubble := false
var action_menu := ActionMenu.new()
var action_library := {}
var _last_work_area := Rect2i()
var _screen_timer := 0.0
var _geometry := false
var _attached_edges := Vector2i(1, 1)
## Vertical motion lives here: a transparent HWND that moves does not make the
## character fall, so gravity and landing are integrated in screen pixels.
var motion := MotionController.new()
var _motion_active := false
var _held_request := -1
var _fall_request := -1
var _recovering := false
var _recovery_remaining := 0.0
var _ground_contact_delay := 0.0
## A press that has not yet travelled far enough to count as a drag. The pet is
## only lifted once `drag.update` reports movement, so a click cannot start one.
var _hold_pending := false
var _press_cursor := Vector2i.ZERO
## Replaces the real desktop rect in headless tests, which report a 0x0 area.
var work_area_override := Rect2i()

## Autonomous behaviour. The controller only decides; everything it decides is
## executed through the same paths a user action would take.
var behavior := BehaviorController.new()
var _behavior_cooldown := 0.0
var _walk_request := -1
## Window offset from the soles anchor, frozen when a walk starts.
##
## Recomputing this per frame from `_subject_rect()` would make the window
## shiver: the skeleton's bounding box changes every frame as the legs swing,
## so both its centre and its bottom edge move a few pixels even though the feet
## are planted. The whole subject rect is frozen, not just x, because `top_left`
## derives from both edges and the clamp below needs a stable rectangle.
var _walk_subject := Rect2()
var _walk_active := false
## Cycles the clip has wrapped through in the current segment. Counted by the
## adapter, which sees the animation position; the window only relays it.
var _walk_cycles_done := 0
## True once the behaviour chain has been switched on. Kept separate from
## `_behavior_cooldown` so a test or a future menu toggle can disable it without
## having to fake a timer.
##
## `--no-behavior` turns the autonomous chain off. The desktop acceptance script
## needs it: that check drags the role by a fixed offset measured from a known
## standing pose, and a pet that walks away on its own moves the hit region out
## from under the injected cursor. Disabling is also what a user who wants the
## pet to stay put would want.
var behavior_enabled := true
## Frame caps from settings.performance, clamped to the service ranges when the
## settings document is applied. The defaults match service/settings.py.
var idle_fps := 30
var drag_fps := 60
var hidden_fps := 5
## `--no-behavior` wins over the saved behavior.enabled value.
var _behavior_forced_off := false

func _ready() -> void:
	_parse_options()
	_behavior_forced_off = options.has("no-behavior")
	if _behavior_forced_off:
		behavior_enabled = false
	if options.has("verify-package"):
		set_process(false)
		_verify_package.call_deferred()
		return
	Engine.max_fps = idle_fps
	get_window().title = "Pet3D Stage 0"
	get_window().close_requested.connect(_quit)
	get_tree().auto_accept_quit = false
	screen = maxi(0, DisplayServer.window_get_current_screen())
	dpi_scale = clampf(DisplayServer.screen_get_dpi(screen) / 96.0, 1.0, 3.0)
	# Install no-activate/passthrough before synchronous model/resource loading.
	if not _initialize_native():
		return
	if DisplayServer.get_name() != "headless":
		get_window().size = Vector2i(Vector2(480, 560) * dpi_scale)
	_recover_position()
	_build_world()
	_geometry = options.has("geometry")
	_load_characters()
	if _geometry or characters.is_empty():
		_build_geometry()
	else:
		_change_character(int(options.get("character", "0")))
	if options.has("diagnostics"):
		_build_diagnostics()
	_configure_action_menu()
	_play_action(str(options.get("action", "idle")))
	_recover_position()
	if bridge:
		# Also consume presses that began on the desktop during model loading.
		drag.update(bridge.is_left_button_down(), false, bridge.get_cursor_position(), get_window().position, false, true)
	print("[stage0] READY ", JSON.stringify(_status()))
	# Stage 1 lifecycle checks also run headless; explicit shutdown owns exit.

## Applies behavior.enabled and performance.* from a settings document.
##
## Called when the service hands settings over, so it must tolerate a missing
## group or an out-of-range value: a pre-v6 service sends neither.
func apply_runtime_settings(settings: Dictionary) -> void:
	var behavior_settings: Variant = settings.get("behavior", {})
	if (behavior_settings is Dictionary and behavior_settings.has("enabled")
			and not _behavior_forced_off):
		var was_enabled := behavior_enabled
		behavior_enabled = bool(behavior_settings.get("enabled", true))
		# Stop a walk the autonomous chain already started; otherwise the pet
		# keeps walking until that segment ends after the switch was turned off.
		if was_enabled and not behavior_enabled and _walk_active:
			_finish_walk(false)
	var performance: Variant = settings.get("performance", {})
	if performance is Dictionary:
		idle_fps = _fps_setting(performance, "idle_fps", idle_fps, 10, 60)
		drag_fps = _fps_setting(performance, "drag_fps", drag_fps, 30, 120)
		hidden_fps = _fps_setting(performance, "hidden_fps", hidden_fps, 1, 15)
	Engine.max_fps = _active_fps()

## Reads an integer fps value, falling back when it is missing or outside the
## range service/settings.py validates.
func _fps_setting(group: Dictionary, key: String, fallback: int, low: int, high: int) -> int:
	var value: Variant = group.get(key)
	if value is int:
		var fps := int(value)
		if low <= fps and fps <= high:
			return fps
	return fallback

func _active_fps() -> int:
	if hidden:
		return hidden_fps
	if drag.active:
		return drag_fps
	return idle_fps

func _configure_action_menu() -> void:
	# Feed the native tray menu the same flat order this class maps commands back to.
	action_library.clear()
	var manifest: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/humanoid_core.json"))
	if manifest is Dictionary:
		var actions: Variant = manifest.get("actions", {})
		if actions is Dictionary:
			action_library = actions
	if action_library.is_empty():
		return
	var built := ActionMenu.build(ActionMenu.load_table(), action_library)
	action_menu.configure(built.groups, built.flat, built.get("labels", {}))
	if bridge and bridge.has_method("set_menu_actions"):
		if not bridge.set_menu_actions(action_menu.native_payload()):
			push_error("Action menu payload rejected")
	print("[stage0] ACTION_MENU groups=", action_menu.group_count(), " actions=", action_menu.actions().size())

func _verify_package() -> void:
	# Release templates omit --script. This explicit headless entry runs the
	# single external verifier while res:// still resolves to this EXE's PCK.
	var path := str(options["verify-package"])
	if DisplayServer.get_name() != "headless" or not path.is_absolute_path() or not FileAccess.file_exists(path):
		push_error("Package verification requires --headless and an absolute verifier script path")
		_quit(2)
		return
	var verifier := load(path) as Script
	if verifier == null or not verifier.has_method("verify_package"):
		push_error("Package verifier could not be loaded")
		_quit(2)
		return
	var code: int = await verifier.verify_package(get_tree())
	_quit(code)

func _initialize_native() -> bool:
	if options.has("no-native") or DisplayServer.get_name() != "Windows":
		return true
	if not ClassDB.class_exists("PetWindowBridge"):
		push_error("Windows window bridge is missing. Rebuild native module before desktop use.")
		_quit(3)
		return false
	bridge = ClassDB.instantiate("PetWindowBridge")
	if not bridge.configure(DisplayServer.window_get_native_handle(DisplayServer.WINDOW_HANDLE)):
		push_error("Could not initialize the native window/tray bridge.")
		_quit(4)
		return false
	bridge.set_interactive(false)
	previous_interactive = false
	dpi_scale = bridge.get_dpi_scale()
	return true

func _parse_options() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--"):
			var pair := arg.substr(2).split("=", true, 1)
			options[pair[0]] = pair[1] if pair.size() == 2 else "true"

func _build_world() -> void:
	camera = Camera3D.new()
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = 3.0
	camera.position = Vector3(0, 0.9, 4)
	add_child(camera)
	camera.look_at(Vector3(0, 0.9, 0))
	camera.make_current()
	var key := DirectionalLight3D.new()
	key.light_energy = 1.5
	key.rotation_degrees = Vector3(-25, -30, 0)
	add_child(key)
	var fill := DirectionalLight3D.new()
	fill.light_energy = 0.5
	fill.light_color = Color(0.78, 0.85, 1)
	fill.rotation_degrees = Vector3(-15, 150, 0)
	add_child(fill)
	var environment := WorldEnvironment.new()
	environment.environment = Environment.new()
	environment.environment.background_mode = Environment.BG_CLEAR_COLOR
	environment.environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.environment.ambient_light_color = Color(0.8, 0.85, 0.95)
	environment.environment.ambient_light_energy = 0.35
	add_child(environment)

func _build_geometry() -> void:
	actor = Node3D.new()
	actor.name = "GeometryProbe"
	add_child(actor)
	var body := MeshInstance3D.new()
	var mesh := CapsuleMesh.new()
	mesh.radius = 0.22
	mesh.height = 1.15
	body.mesh = mesh
	body.position.y = 0.65
	var mat := StandardMaterial3D.new()
	mat.albedo_color = Color(0.25, 0.65, 0.95)
	body.material_override = mat
	actor.add_child(body)
	var head := MeshInstance3D.new()
	var sphere := SphereMesh.new()
	sphere.radius = 0.18
	sphere.height = 0.36
	head.mesh = sphere
	head.position.y = 1.42
	head.material_override = mat
	actor.add_child(head)

func _load_characters() -> void:
	# Asset manifest is produced by the separate repeatable asset build.
	var path := "res://assets_data/characters.json"
	if FileAccess.file_exists(path):
		var data: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
		if data is Array:
			characters = data

func _change_character(index: int) -> void:
	if characters.is_empty():
		return
	var had_actor := is_instance_valid(actor)
	var old_rect := _subject_rect()
	var old_foot := Vector2(get_window().position) + Vector2(old_rect.get_center().x, old_rect.end.y)
	var next_index := posmod(index, characters.size())
	var adapter_script: Script = load("res://scripts/character_view.gd")
	if adapter_script == null:
		return
	var candidate: Node3D = adapter_script.new()
	candidate.process_mode = Node.PROCESS_MODE_DISABLED
	add_child(candidate)
	if not candidate.load_character(characters[next_index]):
		candidate.queue_free()
		push_error("Character failed validation; existing character retained")
		return
	_cancel_drag()
	if is_instance_valid(actor):
		actor.get_parent().remove_child(actor)
		actor.queue_free()
	actor = candidate
	current_character = next_index
	_apply_actor_state()
	actor.action_changed.connect(_on_action_changed)
	_play_action("idle")
	if had_actor:
		var new_rect := _subject_rect()
		get_window().position = Vector2i((old_foot - Vector2(new_rect.get_center().x, new_rect.end.y)).round())
	_constrain_window()
	print("[stage0] CHARACTER ", JSON.stringify(characters[next_index]))

func _apply_actor_state() -> void:
	if not actor:
		return
	if actor.has_method("set_expression"):
		actor.mouth_demo = options.has("mouth-demo")
		actor.blink_override = float(options.get("blink", "-1"))
		actor.mouth_override = float(options.get("mouth", "-1"))
	actor.process_mode = Node.PROCESS_MODE_DISABLED if hidden else Node.PROCESS_MODE_INHERIT

func _process(delta: float) -> void:
	if quitting:
		return
	elapsed += delta
	if elapsed > 2.0 and frame_samples.size() < 18000 and not hidden:
		frame_samples.append(delta * 1000.0)
	if bridge:
		var tray: int = bridge.poll_tray_action()
		_handle_tray(tray)
		if quitting:
			return
	_screen_timer += delta
	if _screen_timer >= 1.0:
		_screen_timer = 0.0
		_check_screen()
	if not hidden:
		_update_motion(delta)
		_update_walk()
		_update_behavior(delta)
		# Foot planting is a standing-state concern, not a falling one. Driving it
		# from `_update_motion` alone meant it never ran again once the pet
		# settled — a walk frees the ankles and nothing put them back.
		_settle_ground_contact(delta)
		_constrain_window()
		_update_regions()
		_update_bubble_position()
		if bridge:
			_update_input()
		if status_label:
			var status := "待机"
			if motion.is_held():
				status = "拖动中"
			elif motion.is_falling():
				status = "下落"
			elif _recovering:
				status = "恢复"
			elif _walk_active:
				status = "行走"
			status_label.text = "Godette · %s\n%s" % [current_action, status]
	if options.has("capture") and elapsed > float(options.get("capture-at", "3")) and not captured_image:
		captured_image = true
		_capture.call_deferred()
	if options.has("auto-exit") and elapsed >= float(options["auto-exit"]):
		_quit()

func _update_regions() -> void:
	# Viewport pixels, same space as the cursor and window position.
	if actor and actor.has_method("project_hit_regions"):
		regions = actor.project_hit_regions(camera)
	else:
		var px := get_window().size.y / camera.size
		regions = [
			{"a": camera.unproject_position(Vector3(0, 0.3, 0)), "b": camera.unproject_position(Vector3(0, 1.0, 0)), "radius": px * 0.23},
			{"a": camera.unproject_position(Vector3(0, 1.42, 0)), "b": camera.unproject_position(Vector3(0, 1.42, 0)), "radius": px * 0.19}
		]


## --- Speech bubble -----------------------------------------------------------
#
# The bubble lives in its own CanvasLayer, above the 3D canvas and the
# diagnostics label. It is a child Control of this window, so it travels with
# the window for free; only the offset above the head is recomputed each frame.

## Shows `text` over the pet. `seconds` below zero uses the automatic duration
## (4 s, longer for long text, capped at 10 s). `reply` clips the text to
## REPLY_BUBBLE_CHARS with an ellipsis; click phrases keep their full text.
func show_bubble(text: String, seconds := -1.0, reply := false) -> void:
	if not _bubble_enabled or text.strip_edges().is_empty():
		hide_bubble()
		return
	_ensure_speech_bubble()
	speech_bubble.set_max_width(_bubble_max_width())
	speech_bubble.set_max_chars(REPLY_BUBBLE_CHARS if reply else 0)
	speech_bubble.show_bubble(text, seconds)
	_update_bubble_position()

func hide_bubble() -> void:
	if is_instance_valid(speech_bubble):
		speech_bubble.hide_bubble()

## Applies the service's bubble.enabled setting. Disabling hides whatever is
## showing and makes every later show a no-op; interaction goes away with it.
func set_bubble_enabled(value: bool) -> void:
	_bubble_enabled = value
	if is_instance_valid(speech_bubble):
		speech_bubble.set("enabled", value)
	if not value:
		hide_bubble()

## Applies the service's bubble.duration_seconds. Click lines and the start
## greeting pass it explicitly, so the setting covers every kind of bubble and
## not only the ones main.gd shows.
func set_bubble_seconds(value: float) -> void:
	_bubble_duration = value

## Applies the service's greeting group. The cooldown range matches
## `_validate_greeting` in service/settings.py; an out-of-range value keeps the
## previous one rather than inventing a cooldown the service would reject.
func set_greeting_settings(enabled: bool, on_start: bool, cooldown_minutes: int) -> void:
	greeting["enabled"] = enabled
	greeting["on_start"] = on_start
	if 5 <= cooldown_minutes and cooldown_minutes <= 1440:
		greeting["cooldown_minutes"] = cooldown_minutes

func _ensure_speech_bubble() -> void:
	if is_instance_valid(speech_bubble):
		return
	_bubble_layer = CanvasLayer.new()
	_bubble_layer.name = "SpeechBubbleLayer"
	# Above the 3D canvas and the diagnostics label, which both use layer 1.
	_bubble_layer.layer = 2
	add_child(_bubble_layer)
	speech_bubble = SpeechBubble.new()
	speech_bubble.name = "SpeechBubble"
	_bubble_layer.add_child(speech_bubble)
	# The bubble is the one control that may take a click; everything else in
	# this window stays click-through. Its rectangle joins the native hit area
	# only while it is visible, so hiding it restores passthrough.
	speech_bubble.set("enabled", _bubble_enabled)
	speech_bubble.set_interactive(true)
	speech_bubble.pressed.connect(_on_bubble_pressed)

## Hides the bubble a click landed on and hands the click to the app layer.
func _on_bubble_pressed() -> void:
	if not is_instance_valid(speech_bubble) or not speech_bubble.visible:
		return
	hide_bubble()
	bubble_clicked.emit()

## True when the cursor is over the visible, interactive bubble. The bubble is a
## child of a CanvasLayer with an identity transform, so its global rect is
## already in the same viewport pixels the cursor is converted into.
func _pointer_in_bubble(cursor: Vector2i) -> bool:
	if not is_instance_valid(speech_bubble) or not speech_bubble.visible:
		return false
	if not bool(speech_bubble.get("interactive")):
		return false
	return speech_bubble.get_global_rect().has_point(Vector2(cursor - get_window().position))

## Widest the bubble may get: never wider than the window that draws it.
func _bubble_max_width() -> float:
	return maxf(1.0, float(get_window().size.x) - BUBBLE_WINDOW_MARGIN * 2.0)

## Pins the bubble above the character's head and inside the work area.
func _update_bubble_position() -> void:
	if not is_instance_valid(speech_bubble) or not speech_bubble.visible:
		return
	var limit := _bubble_max_width()
	if not is_equal_approx(speech_bubble.max_width, limit):
		speech_bubble.set_max_width(limit)
	var bubble_size: Vector2 = speech_bubble.get_bubble_size()
	var subject := _subject_rect()
	var window := get_window()
	var origin := Vector2(window.position)
	var area_rect := _work_area()
	var area := Rect2(Vector2(area_rect.position), Vector2(area_rect.size))
	var head := origin + Vector2(subject.get_center().x, subject.position.y)
	var target := Vector2(head.x - bubble_size.x * 0.5, head.y - bubble_size.y - BUBBLE_GAP)
	var on_screen := Vector2(
		clampf(target.x, area.position.x, maxf(area.position.x, area.end.x - bubble_size.x)),
		clampf(target.y, area.position.y, maxf(area.position.y, area.end.y - bubble_size.y)))
	# Clamping to the work area alone is not enough: the bubble is drawn inside
	# this window, so a pet standing against an edge would push it outside the
	# viewport and clip it. Keep it inside both.
	var local := on_screen - origin
	local.x = clampf(local.x, 0.0, maxf(0.0, float(window.size.x) - bubble_size.x))
	local.y = clampf(local.y, 0.0, maxf(0.0, float(window.size.y) - bubble_size.y))
	speech_bubble.position = local.round()


func _update_input() -> void:
	# Every value here is already in the same space: the bridge reads the cursor
	# with GetPhysicalCursorPos, get_window().position is physical too (verified
	# with SetWindowPos), and the camera projects to viewport pixels, which are
	# physical because the viewport is not stretched. Scaling any one of them by
	# dpi_scale breaks the hit test at 150% display scaling.
	var cursor: Vector2i = bridge.get_cursor_position()
	var down: bool = bridge.is_left_button_down()
	var work_area := _work_area()
	var window_position := get_window().position
	var subject := _subject_rect()
	var inside := ScreenMath.pointer_in_subject(cursor, window_position, subject, work_area)
	var on_body := inside and HitRegions.contains(Vector2(cursor - window_position), regions)
	var bubble_hit := _pointer_in_bubble(cursor)
	var hit := on_body or bubble_hit
	var state: Dictionary = drag.update(down, hit, cursor, get_window().position, bridge.has_capture(), true)
	var interactive: bool = hit or drag.active
	if interactive != previous_interactive:
		bridge.set_interactive(interactive)
		previous_interactive = interactive
	if state.begin:
		# Remember where the press landed: a press that started on the bubble is
		# a click on it, never a drag of the pet. A bubble that happens to be
		# drawn over the body (a very narrow window) does not take that press;
		# the character keeps priority there.
		_press_on_bubble = bubble_hit and not on_body
		if not bridge.begin_capture():
			drag.cancel()
			_press_on_bubble = false
		else:
			Engine.max_fps = drag_fps
			# A press is not a grab yet. Only travel past the drag threshold turns
			# it into one, so a plain click never lifts the pet and drops it.
			_press_cursor = cursor
			_hold_pending = true
	if state.moved and not _press_on_bubble:
		if _hold_pending:
			_hold_pending = false
			print("[stage0] DRAG begin")
			_begin_hold(_press_cursor)
		_hold_to(cursor)
	if state.end:
		bridge.end_capture()
		Engine.max_fps = idle_fps
		if _press_on_bubble:
			_press_on_bubble = false
			_hold_pending = false
			_on_bubble_pressed()
		elif _hold_pending:
			_hold_pending = false
			print("[stage0] CLICK position=", get_window().position)
			# A click is a user instruction: stop walking and answer it.
			_finish_walk(false)
			_play_action("wave")
			_say_click_phrase()
		else:
			print("[stage0] DRAG end position=", get_window().position)
			_end_hold()


## Says one greeting or care phrase when the pet is clicked. The library is
## optional: a missing or unreadable file leaves the click silent instead of
## failing, so a package without the data folder still works.
func _say_click_phrase() -> void:
	var phrase := _pick_click_phrase()
	if not phrase.is_empty():
		show_bubble(phrase, _bubble_duration)

## Says one greeting phrase after the window first appears, when the service's
## greeting.on_start is on.
##
## Starting is not a click, so the click cooldown does not apply here. The
## phrase still has to match the wall-clock window, and `show_bubble` keeps
## honouring bubble.enabled.
func greet_on_start() -> void:
	if not _greetings_on() or not bool(greeting.get("on_start", true)):
		return
	var phrase := _pick_start_phrase()
	if not phrase.is_empty():
		show_bubble(phrase, _bubble_duration)

## Picks one click phrase valid for the current wall-clock window.
##
## The greeting switch silences the click entirely. The cooldown keeps a second
## click from replacing the words of a recent one: the pet still waves, it just
## does not speak again. Time windows are half-open and match
## service/expression/phrases.py, so "早上好" never shows at midnight.
func _pick_click_phrase() -> String:
	if not _greetings_on():
		return ""
	var now := _greeting_now()
	if not _click_greeting_ready(float(now.get("minutes", 0.0))):
		return ""
	var choices := _phrase_choices(CLICK_PHRASE_CATEGORIES, time_window_for(int(now.get("hour", 0))))
	if choices.is_empty():
		return ""
	_click_greeting_said = true
	_click_greeting_minutes = float(now.get("minutes", 0.0))
	return choices[randi() % choices.size()]

## Picks one enabled start greeting for the current wall-clock window.
func _pick_start_phrase() -> String:
	var hour := int(_greeting_now().get("hour", 0))
	var choices := _phrase_choices(START_PHRASE_CATEGORIES, time_window_for(hour))
	if choices.is_empty():
		return ""
	return choices[randi() % choices.size()]

## Every enabled phrase in `categories` whose own `time_window` covers `window`.
##
## The first readable library with usable entries wins, as before; a local file
## that has nothing for this hour falls through to the packaged one.
func _phrase_choices(categories: Array, window: String) -> Array[String]:
	var choices: Array[String] = []
	for path in _phrase_paths():
		if not FileAccess.file_exists(path):
			continue
		var data: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
		if not (data is Dictionary):
			continue
		var entries: Variant = (data as Dictionary).get("phrases", [])
		if not (entries is Array):
			continue
		choices.clear()
		for item in entries:
			if not (item is Dictionary):
				continue
			var entry: Dictionary = item
			if not categories.has(str(entry.get("category", ""))):
				continue
			if not bool(entry.get("enabled", true)):
				continue
			if not _phrase_window_matches(entry, window):
				continue
			var text := str(entry.get("text", "")).strip_edges()
			if not text.is_empty():
				choices.append(text)
		if not choices.is_empty():
			return choices
	return choices

## Window name of one wall-clock hour, matching `time_window_for` in
## service/expression/phrases.py.
static func time_window_for(hour: int) -> String:
	for name in ["morning", "afternoon", "evening"]:
		var span: Vector2i = TIME_WINDOWS[name]
		if span.x <= hour and hour < span.y:
			return name
	return "night"

## True when a phrase's own `time_window` is valid at `window`. A missing or
## unknown value means every hour, exactly like the service normalizes it.
func _phrase_window_matches(entry: Dictionary, window: String) -> bool:
	var value := str(entry.get("time_window", TIME_WINDOW_ANY))
	if value != TIME_WINDOW_ANY and not TIME_WINDOWS.has(value):
		value = TIME_WINDOW_ANY
	return value == TIME_WINDOW_ANY or value == window

## True when the last click phrase is old enough to be replaced.
func _click_greeting_ready(minutes: float) -> bool:
	if not _click_greeting_said:
		return true
	return minutes - _click_greeting_minutes >= float(greeting.get("cooldown_minutes", 60))

func _greetings_on() -> bool:
	return bool(greeting.get("enabled", true))

## Current wall clock for the greeting rules: `hour` plus `minutes` on one
## timeline. The injectable callable keeps tests off the real clock.
func _greeting_now() -> Dictionary:
	if greeting_clock.is_valid():
		var value: Variant = greeting_clock.call()
		if value is Dictionary:
			var injected: Dictionary = value
			return {
				"hour": int(injected.get("hour", 0)),
				"minutes": float(injected.get("minutes", 0.0)),
			}
	var datetime := Time.get_datetime_dict_from_system()
	return {
		"hour": int(datetime.get("hour", 0)),
		"minutes": Time.get_unix_time_from_system() / 60.0,
	}

## The phrase library ships outside `res://`, in `data/`. Both the development
## tree and a package that copies that folder next to the executable are tried.
func _phrase_paths() -> PackedStringArray:
	var paths := PackedStringArray([ProjectSettings.globalize_path("res://../data/expression/phrases.json")])
	var executable_dir := OS.get_executable_path().get_base_dir()
	if not executable_dir.is_empty():
		paths.append(executable_dir.path_join("data/expression/phrases.json"))
	return paths


## --- Hold / fall / land -------------------------------------------------------
#
# The window follows the motion controller instead of the cursor: while held the
# character swings after the cursor with follow + damping, on release it keeps
# its velocity and falls until the soles reach the work area's bottom edge.

func _ensure_motion_bounds() -> void:
	# Go through `_work_area()` so the headless override (and any future
	# synthetic desktop) reaches the motion controller too. Asking DisplayServer
	# directly here would silently replace a test's bounds with the 0x0 headless
	# rect and clamp the anchor into a corner.
	var work := _work_area()
	_last_work_area = work
	var subject := _subject_rect()
	motion.set_bounds(work, subject.size)
	_sync_motion_anchor(subject)


## Aligns the controller with where the window already is. Only valid when
## nothing owns the anchor: a walk or a fall must never be reset onto the
## window, because the window follows the anchor and not the other way round.
##
## The whole soles point is taken, not just x. A restored placement can leave
## the pet standing above the bottom edge (`placement.y < 1`), and snapping y to
## the ground line here made the pet drop a full body height the moment it was
## grabbed — the desktop drag check caught it as a 382px jump on mouse-down.
func _sync_motion_anchor(subject: Rect2) -> void:
	if _motion_active or _walk_active:
		return
	if motion.is_held() or motion.is_falling():
		return
	var soles := Vector2(get_window().position) + Vector2(subject.get_center().x, subject.end.y)
	motion.place_at(soles)


func _begin_hold(cursor: Vector2i) -> void:
	# Grabbing outranks everything: stop any autonomous walk before the body
	# leaves the ground, or the plan would keep writing anchor.x mid-air.
	_finish_walk(false)
	_ensure_motion_bounds()
	var subject := _subject_rect()
	var soles := Vector2(get_window().position) + Vector2(subject.get_center().x, subject.end.y)
	# Grab height is how far the soles sit below the point clicked on the body.
	var grab_height := clampf(soles.y - float(cursor.y), 0.0, maxf(0.0, subject.size.y))
	motion.begin_hold(Vector2(cursor), grab_height)
	_motion_active = true
	_recovering = false
	if is_instance_valid(actor) and actor.has_method("set_ground_contact"):
		actor.set_ground_contact(false)
	_held_request = _request_body_action("pickup", "drag", 1.0, 1, true)
	print("[stage0] HOLD begin soles=", soles, " grab_height=", grab_height, " request=", _held_request)


func _hold_to(cursor: Vector2i) -> void:
	# Only the cursor target is passed here. Integration stays in `_process`, so a
	# frame with several input samples cannot advance the spring more than once.
	if not motion.is_held():
		return
	motion.steer(Vector2(cursor))
	_apply_motion_to_window()


func _end_hold() -> void:
	if not motion.is_held():
		return
	motion.release()
	# The hold action outranks the fall in the priority table, so it must be
	# released first; otherwise the fall request is rejected and the pet drops
	# with its lifted pose still latched.
	if _held_request > 0 and is_instance_valid(actor) and actor.has_method("release_action"):
		actor.release_action(_held_request)
	_held_request = -1
	var falling_requested := _request_body_action("fall", "fall", 1.0, 1, true)
	if falling_requested < 0:
		print("[stage0] FALL action unavailable; physics only")
	_fall_request = falling_requested
	print("[stage0] HOLD release -> FALL speed=", motion.velocity, " request=", _fall_request)


## Requests an action and returns its request id (-1 when it was rejected).
func _request_body_action(name_: String, source: String, speed := 1.0, direction := 1, hold := false) -> int:
	if not is_instance_valid(actor) or not actor.has_method("request_action"):
		return -1
	return int(actor.request_action(name_, source, speed, direction, hold))


## Drives the released character down to the desktop and hands control back.
func _update_motion(delta: float) -> void:
	if not _motion_active:
		return
	motion.update(delta)
	_apply_motion_to_window()
	_update_body_feedback(delta)
	if motion.take_landing():
		_on_landed()
	if _recovering:
		_recovery_remaining -= delta
		if _recovery_remaining <= 0.0:
			_recovering = false
	# Hand the window back to the pinning logic only once the pet is standing on
	# the ground with the recovery done. Checking velocity alone is not enough:
	# a held pet and a just-released one both report zero velocity for a frame.
	if motion.is_grounded() and motion.is_resting() and not _recovering:
		_motion_active = false


## --- Walking ------------------------------------------------------------------
#
# The window translates; the clip only steps in place. Position comes from the
# animation's own playback progress, so the ground speed is exactly the clip's
# stride rate and the soles do not skate.

## Ground travel per animation cycle, converted with the camera projection.
## Measuring the projection also respects Camera3D's keep-aspect setting; its
## `size` is not necessarily the vertical span of the window.
func _walk_stride_px() -> float:
	if camera == null or not is_instance_valid(actor):
		return 0.0
	var meters := 0.0
	if actor.has_method("measure_walk_stride"):
		meters = float(actor.measure_walk_stride())
	if meters <= 0.0:
		# Fall back to the contract only when the clip cannot be measured, so a
		# character without a skeleton still walks instead of freezing.
		if actor.get("config") is Dictionary:
			var locomotion: Variant = (actor.config as Dictionary).get("locomotion", {})
			if locomotion is Dictionary:
				meters = float(locomotion.get("meters_per_cycle", 0.0))
	if meters <= 0.0:
		return 0.0
	var origin: Vector3 = actor.global_position
	var px_per_unit := camera.unproject_position(origin + Vector3.RIGHT).distance_to(camera.unproject_position(origin))
	return meters * px_per_unit


## Begins a walk segment in `direction`. Returns false when it cannot start.
func _begin_walk(direction: int, cycles: int) -> bool:
	if not is_instance_valid(actor) or not actor.has_method("begin_walk_tracking"):
		return false
	if motion.is_held() or motion.is_falling() or _motion_active:
		return false
	if not motion.is_grounded():
		return false
	# This stage walks along the bottom edge only, and `begin_walk` pins the soles
	# to the ground line. A pet left floating by a saved placement would be
	# teleported a full body height the instant the chain decided to walk, so it
	# has to be standing on that line already. Dragging and releasing it puts it
	# there; until then it idles instead of jumping.
	if absf(motion.anchor.y - motion.ground_y()) > 0.5:
		return false
	# The walk clip replaces whatever is playing; only start from idle so an
	# ambient action is never cut off mid-pose.
	if current_action != "idle":
		return false
	# Placement/scale changes move the window before the motion controller runs.
	# Sync that actual starting point; headless tests supply the anchor explicitly.
	var work := _work_area()
	_last_work_area = work
	if DisplayServer.get_name() != "headless":
		_sync_motion_anchor(_subject_rect())
	motion.set_bounds(work, _subject_rect().size)
	var stride := _walk_stride_px()
	var cycle_seconds := float(actor.walk_cycle_length())
	if stride <= 0.0 or cycle_seconds <= 0.0:
		return false
	var plan: Dictionary = motion.begin_walk(direction, cycles, stride, cycle_seconds)
	if plan.is_empty():
		return false
	# Foot planting fights a moving body: the ankles would be locked to the
	# ground while the hips travel. Let the clip carry the feet instead.
	if actor.has_method("set_ground_contact"):
		actor.set_ground_contact(false)
	actor.begin_walk_tracking()
	if actor.has_method("face_direction"):
		actor.face_direction(direction)
	_walk_cycles_done = 0
	_walk_active = true
	# Freeze the subject rect for the whole segment: its bottom edge feeds the
	# window y and its centre feeds the window x, so a breathing gait box would
	# move the window up and down every frame.
	_walk_subject = _subject_rect()
	_walk_request = _request_body_action("walk", "autonomous", 1.0, 1, false)
	if _walk_request < 0:
		# Without the clip there is no progress to drive, so there is no walk.
		_finish_walk(false)
		return false
	print("[stage0] WALK begin dir=", direction, " cycles=", plan.cycles, " stride=", stride, " length=", cycle_seconds)
	return true


## Advances a running walk from the clip's own position.
func _update_walk() -> void:
	if not _walk_active:
		return
	# Anything that takes the body over ends the walk immediately: the plan would
	# otherwise keep writing anchor.x while a drag or a fall owns the soles.
	if motion.is_held() or motion.is_falling() or not motion.is_grounded():
		_finish_walk(false)
		return
	if not is_instance_valid(actor) or not actor.has_method("walk_cycles_advanced"):
		_finish_walk(false)
		return
	if current_action != "walk":
		# A user action outranks the autonomous walk; the controller already
		# replaced the clip, so stop driving the position.
		_finish_walk(false)
		return
	_walk_cycles_done += int(actor.walk_cycles_advanced())
	# A hitch can let the clip wrap more than once worth of frames between two
	# updates; the adapter reports one wrap per call, but a slow frame can also
	# arrive after the segment already ended. Never let the counter run past the
	# plan, or `advance_walk` would clamp the position and the body would stop
	# short of the target.
	_walk_cycles_done = mini(_walk_cycles_done, int(motion.walk_cycles_total()))
	var length := float(actor.walk_cycle_length())
	var position := float(actor.walk_cycle_position())
	if length <= 0.0 or position < 0.0:
		_finish_walk(false)
		return
	var intra := clampf(position / length, 0.0, 1.0)
	if motion.advance_walk(_walk_cycles_done, intra):
		_apply_walk_to_window()
		_finish_walk(true)
		return
	_apply_walk_to_window()


## Places the window from the walk anchor using the frozen subject rect.
##
## Both axes come from the same frozen rect. Deriving y from a live
## `_subject_rect()` made the window bob with the gait, which is exactly the
## shiver the frozen offset was supposed to prevent.
func _apply_walk_to_window() -> void:
	if DisplayServer.get_name() == "headless" or camera == null:
		return
	var frozen := _walk_subject
	var top_left := Vector2(motion.anchor.x - frozen.get_center().x, motion.anchor.y - frozen.end.y)
	get_window().position = ScreenMath.clamp_subject(Vector2i(top_left.round()), frozen, _work_area())


## Ends a walk segment. `completed` false means it was interrupted.
func _finish_walk(completed: bool) -> void:
	if not _walk_active:
		motion.cancel_walk()
		return
	_walk_active = false
	motion.cancel_walk()
	_walk_request = -1
	if is_instance_valid(actor):
		if actor.has_method("release_walk_facing"):
			actor.release_walk_facing()
		if actor.has_method("set_ground_contact"):
			# Re-plant the feet only once the pose is upright again; the settle
			# helper already enforces that delay on the next frames.
			_ground_contact_delay = 0.2
	_attached_edges = ScreenMath.edge_flags(get_window().position, _subject_rect(), _work_area())
	print("[stage0] WALK end completed=", completed, " position=", get_window().position)
	if completed:
		_request_body_action("idle", "recovery", 1.0, 1, false)
		# Subclasses persist the position; the stage-0 base has nowhere to save.
		if has_method("_queue_position_save"):
			call("_queue_position_save")


## --- Autonomous behaviour -----------------------------------------------------
#
# The controller decides; this only enforces when a decision may be made and
# then routes it through the normal action / walk paths.

func _update_behavior(delta: float) -> void:
	if not behavior_enabled:
		return
	if _behavior_cooldown > 0.0:
		_behavior_cooldown -= delta
		return
	# A held, falling, walking or recovering pet is already busy. Deciding now
	# would either be discarded or cut an action off mid-clip.
	if _walk_active or _motion_active or _recovering:
		return
	if motion.is_held() or motion.is_falling():
		return
	if current_action != "idle":
		return
	var available := _behavior_action_table()
	var stride := _walk_stride_px()
	var room := _walk_room()
	var decision: Dictionary = behavior.decide(available, float(room.left), float(room.right), stride)
	_behavior_cooldown = maxf(0.1, float(decision.get("cooldown", 3.0)))
	match str(decision.get("kind", BehaviorController.KIND_IDLE)):
		BehaviorController.KIND_ACTION:
			_play_action(str(decision.get("action", "")))
		BehaviorController.KIND_WALK:
			if not _begin_walk(int(decision.get("direction", 0)), int(decision.get("cycles", 0))):
				# The plan could not be realised (no clip, no stride, clamped to
				# zero); wait out the cooldown rather than retrying every frame.
				pass


## Action metadata the behaviour chain needs: mode plus clip length.
##
## `humanoid_core.json` carries the mode but not the length, which lives in the
## baked AnimationLibrary, so the two sources are merged here.
func _behavior_action_table() -> Dictionary:
	var table := {}
	for name in action_library.keys():
		var entry: Variant = action_library[name]
		if not (entry is Dictionary):
			continue
		var info := (entry as Dictionary).duplicate()
		info["length"] = _action_length(str(name))
		table[str(name)] = info
	return table


## Free room from the body's centre to each wall of the work area, in pixels.
##
## Measured from the soles anchor, not the window: the transparent border is not
## part of the body, and the anchor is what the walk plan actually moves.
func _walk_room() -> Dictionary:
	var area := _work_area()
	var subject := _subject_rect()
	var half_width := subject.size.x * 0.5
	var center := Vector2(get_window().position).x + subject.get_center().x
	if DisplayServer.get_name() == "headless":
		center = motion.anchor.x
	var area_right := float(area.position.x) + float(area.size.x)
	return {
		"left": maxf(0.0, center - half_width - float(area.position.x)),
		"right": maxf(0.0, area_right - (center + half_width)),
	}


## Re-enables foot planting after landing, but only once the pose has had time to
## come back upright; locking the ankles during the fall or recovery pose drags
## the body flat.
func _settle_ground_contact(delta: float) -> void:
	if not is_instance_valid(actor) or not actor.has_method("set_ground_contact"):
		return
	if motion.is_held() or motion.is_falling():
		return
	# A travelling body must not have its ankles locked: the hips move while the
	# plan says the feet are planted, which folds the legs. The walk frees them
	# on start and this delay covers the hand-back afterwards.
	if _walk_active:
		if actor.ground_contact_enabled():
			actor.set_ground_contact(false)
		return
	if _ground_contact_delay > 0.0:
		_ground_contact_delay -= delta
		return
	# Only upright actions keep their feet planted. A crouched recovery, a fall
	# crouch or any other bent pose has its ankles nowhere near the idle height,
	# and forcing them there folds the legs inside out.
	if not _upright_action():
		if actor.ground_contact_enabled():
			actor.set_ground_contact(false)
		return
	if not actor.ground_contact_enabled():
		actor.set_ground_contact(true, 1.0)


## True while the current pose is close enough to standing for ankle locking.
##
## `walk` is deliberately absent. The correction only rotates the upper and
## lower leg to pull the ankle to a target height, so applying it mid-stride
## fights the clip: the shin is bent away from the pose the animation authored
## and the foot, which is a child of the shin, ends up twisted. While walking the
## clip owns the legs.
func _upright_action() -> bool:
	return current_action in ["idle", "run", "turn", "wave", "agreeing", "angry", "bashful", "bored"]


func _apply_motion_to_window() -> void:
	if DisplayServer.get_name() == "headless" or camera == null:
		return
	var subject := _subject_rect()
	# anchor is the soles point; convert it back into a window top-left.
	var top_left := Vector2(motion.anchor.x - subject.get_center().x, motion.anchor.y - subject.end.y)
	var clamped := ScreenMath.clamp_subject(Vector2i(top_left.round()), subject, DisplayServer.screen_get_usable_rect(screen))
	get_window().position = clamped


## Feeds the visible shove to the spring bones and matches the step cycle speed.
func _update_body_feedback(_delta: float) -> void:
	if not is_instance_valid(actor):
		return
	if actor.has_method("set_body_inertia"):
		var inertia: Vector3 = motion.inertia()
		# While airborne the character is rotating in the world, not being pushed;
		# only the drag phase is a body shove.
		if motion.is_falling():
			inertia = Vector3.ZERO
		actor.set_body_inertia(inertia)
	if actor.has_method("apply_locomotion_speed"):
		var horizontal: float = absf(motion.velocity.x)
		var nominal := 1.0
		var locomotion: Dictionary = actor.config.get("locomotion", {}) if actor.get("config") != null else {}
		if locomotion.get("nominal_speed") is float and float(locomotion.get("nominal_speed")) > 0.1:
			nominal = float(locomotion.get("nominal_speed"))
		actor.apply_locomotion_speed(clampf(horizontal / maxf(1.0, nominal * 30.0), 0.6, 2.0))


func _on_landed() -> void:
	print("[stage0] LANDED speed=", motion.last_landing_speed(), " soles=", motion.anchor)
	# Recompute the edge attachment from where the pet actually landed. Forcing a
	# fixed edge here used to snap every landing back to the bottom-right corner,
	# which made a drag look like it had been undone.
	_attached_edges = ScreenMath.edge_flags(get_window().position, _subject_rect(), DisplayServer.screen_get_usable_rect(screen))
	# The fall holds its pose until released; release it before asking for the
	# recovery or idle, or the higher-priority fall keeps the request rejected.
	if _fall_request > 0 and is_instance_valid(actor) and actor.has_method("release_action"):
		actor.release_action(_fall_request)
	_fall_request = -1
	if motion.needs_recovery():
		var request := _request_body_action("recover", "recovery", 1.0, 1, false)
		_recovering = request >= 0
		_recovery_remaining = 0.0 if request < 0 else _action_length("recover")
	else:
		_recovering = false
		_request_body_action("idle", "recovery", 1.0, 1, false)
	# Foot planting only makes sense once the body is upright. Enabling it while
	# the fall/recover pose is still bent over would pull the ankles to the idle
	# height and flatten the whole body. `_settle_ground_contact` fades it in as
	# the pose returns to standing.
	_ground_contact_delay = 0.0 if _recovering else 0.25


func _action_length(name_: String) -> float:
	if not is_instance_valid(actor) or not actor.get("player"):
		return 0.3
	var animation: Animation = actor.player.get_animation("shared/" + name_)
	return animation.length if animation != null else 0.3


func _handle_tray(action: int) -> void:
	var chosen := action_menu.action_for_command(action)
	if not chosen.is_empty():
		_play_action(chosen)
		return
	match action:
		1:
			_cancel_drag()
			if is_instance_valid(actor):
				actor.reset_performance_state()
			hidden = not hidden
			# Godot cannot hide its main Window. Hide its transparent contents;
			# native passthrough below keeps the empty HWND out of mouse input.
			visible = not hidden
			if status_label:
				status_label.visible = not hidden
			# The bubble sits on its own canvas, so it does not inherit the 3D
			# node's visibility and has to be hidden explicitly.
			if is_instance_valid(_bubble_layer):
				_bubble_layer.visible = not hidden
			_apply_actor_state()
			Engine.max_fps = hidden_fps if hidden else idle_fps
			if bridge:
				bridge.set_interactive(false)
				previous_interactive = false
				drag.update(bridge.is_left_button_down(), false, bridge.get_cursor_position(), get_window().position, false, false)
			print("[stage0] HIDDEN ", hidden)
		2:
			_recover_position()
			if hidden:
				_handle_tray(1)
		4:
			_play_action("wave" if current_action == "idle" else "idle")
		5:
			_quit()

func _play_action(action: String) -> void:
	# Any action request replaces the walk clip, so the plan must stop driving
	# the position in the same frame. The behaviour chain only ever asks for an
	# action while standing idle, so this cannot cut a walk short from inside.
	if _walk_active and action != "walk":
		_finish_walk(false)
	if actor and actor.has_method("play_action"):
		actor.play_action(action)
	else:
		_on_action_changed(action)

func _on_action_changed(action: String) -> void:
	current_action = action
	print("[stage0] ACTION ", action)


## Plays an animation a chat reply asked for.
##
## Chat ranks below the user and below the physics states in `SOURCE_PRIORITY`,
## so a reply that arrives mid-drag, mid-fall or while a menu action is playing
## is dropped rather than cutting into it. Only the library's one-shot clips are
## played as clips; `walk` becomes a real walk segment so the feet stay planted
## to the travelling window instead of stepping on the spot.
func _play_chat_action(action: String) -> void:
	if action.is_empty() or action == "none":
		return
	if not action_library.has(action):
		# The service validates against the same library, but a stale service or
		# a newer app must not make the pet falter on an id it does not have.
		return
	if motion.is_held() or motion.is_falling() or _recovering:
		return
	if action == "walk":
		_begin_chat_walk()
		return
	# Only one-shot clips: a loop would never complete, and a hold clip latches
	# until something releases it — the chat path has no release step.
	if str((action_library[action] as Dictionary).get("mode", "once")) != "once":
		return
	if _walk_active:
		_finish_walk(false)
	_request_body_action(action, "chat")


## Starts a short walk in the direction with room, from a chat request.
func _begin_chat_walk() -> void:
	var room := _walk_room()
	var stride := _walk_stride_px()
	var direction := 1 if float(room.right) >= float(room.left) else -1
	if maxf(float(room.left), float(room.right)) < stride:
		return
	_begin_walk(direction, 2)

func _check_screen() -> void:
	if DisplayServer.get_name() == "headless":
		return
	if screen >= DisplayServer.get_screen_count():
		screen = DisplayServer.get_primary_screen()
	var work := DisplayServer.screen_get_usable_rect(screen)
	if work != _last_work_area:
		_cancel_drag()
		_last_work_area = work
		_constrain_window()

func _recover_position() -> void:
	if DisplayServer.get_name() == "headless":
		return
	_cancel_drag()
	var work := DisplayServer.screen_get_usable_rect(screen)
	_last_work_area = work
	# Keep the character inside the work area without snapping it to an edge: a
	# blank recovery is what should happen on "restore position", not a teleport
	# into the bottom-right corner where the body can sit off-screen.
	get_window().position = ScreenMath.clamp_subject(get_window().position, _subject_rect(), work)
	_attached_edges = ScreenMath.edge_flags(get_window().position, _subject_rect(), work)

## The desktop area the pet may occupy, in physical pixels. Headless has no
## display, so `work_area_override` lets tests exercise the hit test.
func _work_area() -> Rect2i:
	if work_area_override.size.x > 0 and work_area_override.size.y > 0:
		return work_area_override
	return DisplayServer.screen_get_usable_rect(screen)


func _subject_rect() -> Rect2:
	# Viewport pixels. The cursor, the window position and this rect all live in
	# physical pixels, so nothing here is scaled by dpi_scale.
	if camera == null or not is_instance_valid(actor):
		return Rect2(Vector2.ZERO, Vector2(get_window().size))
	if actor.has_method("project_visible_rect"):
		return actor.project_visible_rect(camera)
	var rectangle := Rect2()
	var first := true
	for mesh: MeshInstance3D in actor.find_children("*", "MeshInstance3D", true, false):
		var box: AABB = mesh.global_transform * mesh.get_aabb()
		for i in 8:
			var point := camera.unproject_position(box.get_endpoint(i))
			rectangle = Rect2(point, Vector2.ZERO) if first else rectangle.expand(point)
			first = false
	return rectangle if not first else Rect2(Vector2.ZERO, Vector2(get_window().size))

func _constrain_window() -> void:
	if DisplayServer.get_name() == "headless" or camera == null:
		return
	# While the motion controller owns the position — a fall, or a walk driving
	# `anchor.x` — pinning the subject to an edge every frame would fight it and
	# drag the character back to where it started.
	if _motion_active or _walk_active:
		return
	var work := _work_area()
	var subject := _subject_rect()
	# Only keep the character reachable: clamp it inside the work area, but never
	# pull it toward an edge it is not already touching. Re-pinning every frame
	# against a moving idle bounds box made the window drift on its own.
	var clamped := ScreenMath.clamp_subject(get_window().position, subject, work)
	if clamped != get_window().position:
		get_window().position = clamped
		_attached_edges = ScreenMath.edge_flags(clamped, subject, work)

func _cancel_drag() -> void:
	var was_held := motion.is_held()
	drag.cancel()
	# A press that never travelled is dropped with the capture; leaving the flag
	# set would turn the next unrelated release into a click.
	_hold_pending = false
	_press_on_bubble = false
	if was_held:
		# A cancelled drag must not leave the pet hanging in mid-air.
		motion.release()
		_motion_active = true
	if bridge:
		bridge.end_capture()
	Engine.max_fps = hidden_fps if hidden else idle_fps

func _build_diagnostics() -> void:
	var layer := CanvasLayer.new()
	add_child(layer)
	status_label = Label.new()
	status_label.position = Vector2(12, 12)
	status_label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	status_label.add_theme_color_override("font_color", Color.WHITE)
	status_label.add_theme_color_override("font_outline_color", Color.BLACK)
	status_label.add_theme_constant_override("outline_size", 5)
	layer.add_child(status_label)

func _character_name() -> String:
	return str(characters[current_character].get("name", "3D 角色")) if not characters.is_empty() and not _geometry else "几何窗口验证"

func _status() -> Dictionary:
	var data := {"engine": Engine.get_version_info().string, "renderer": RenderingServer.get_current_rendering_method(), "driver": RenderingServer.get_current_rendering_driver_name(), "adapter": RenderingServer.get_video_adapter_name(), "dpi_scale": dpi_scale, "window_position": [get_window().position.x, get_window().position.y], "window_size": [get_window().size.x, get_window().size.y], "native": bridge != null, "character": _character_name(), "action": current_action, "elapsed": elapsed, "pid": OS.get_process_id(), "frames_sampled": frame_samples.size()}
	if not frame_samples.is_empty():
		var sorted := frame_samples.duplicate()
		sorted.sort()
		# Includes the frame limiter's sleep; this is pacing, not render cost.
		data["frame_interval_p95_ms"] = sorted[mini(sorted.size() - 1, int(sorted.size() * 0.95))]
	if actor and actor.has_method("describe"):
		data["character_details"] = actor.describe()
	var subject := _subject_rect()
	data["visible_rect"] = [subject.position.x, subject.position.y, subject.size.x, subject.size.y]
	data["edge_attachment"] = [_attached_edges.x, _attached_edges.y]
	if DisplayServer.get_name() != "headless":
		var work := DisplayServer.screen_get_usable_rect(screen)
		data["work_area"] = [work.position.x, work.position.y, work.size.x, work.size.y]
		data["foot_gap_px"] = work.end.y - (get_window().position.y + subject.end.y)
		data["right_gap_px"] = work.end.x - (get_window().position.x + subject.end.x)
	return data

func _capture() -> void:
	await RenderingServer.frame_post_draw
	var result := get_viewport().get_texture().get_image().save_png(str(options["capture"]))
	print("[stage0] CAPTURE result=", result)
	if options.has("report"):
		_write_report()

func _write_report() -> void:
	var file := FileAccess.open(str(options["report"]), FileAccess.WRITE)
	if file:
		file.store_string(JSON.stringify(_status(), "\t"))
	else:
		push_error("Could not write stage0 report")

func _quit(code: int = 0) -> void:
	if quitting:
		return
	quitting = true
	if is_instance_valid(actor) and actor.has_method("reset_performance_state"):
		actor.reset_performance_state()
	_cancel_drag()
	if options.has("report"):
		_write_report()
	if bridge:
		bridge.shutdown()
		bridge = null
	print("[stage0] EXIT code=", code)
	get_tree().quit(code)

func _exit_tree() -> void:
	if bridge:
		bridge.shutdown()
