extends Node3D
## Stage 0 application: rendering/interaction and native lifetime only.

const DragState = preload("res://scripts/drag_state.gd")
const HitRegions = preload("res://scripts/hit_regions.gd")
const ScreenMath = preload("res://scripts/screen_math.gd")
const ActionMenu = preload("res://scripts/action_menu.gd")
const MotionController = preload("res://scripts/motion/motion_controller.gd")

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

func _ready() -> void:
	_parse_options()
	if options.has("verify-package"):
		set_process(false)
		_verify_package.call_deferred()
		return
	Engine.max_fps = 30
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
		_constrain_window()
		_update_regions()
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
	var on_body := HitRegions.contains(Vector2(cursor - window_position), regions)
	var hit := inside and on_body
	var state: Dictionary = drag.update(down, hit, cursor, get_window().position, bridge.has_capture(), true)
	var interactive: bool = hit or drag.active
	if interactive != previous_interactive:
		bridge.set_interactive(interactive)
		previous_interactive = interactive
	if state.begin:
		if not bridge.begin_capture():
			drag.cancel()
		else:
			Engine.max_fps = 60
			# A press is not a grab yet. Only travel past the drag threshold turns
			# it into one, so a plain click never lifts the pet and drops it.
			_press_cursor = cursor
			_hold_pending = true
	if state.moved:
		if _hold_pending:
			_hold_pending = false
			print("[stage0] DRAG begin")
			_begin_hold(_press_cursor)
		_hold_to(cursor)
	if state.end:
		bridge.end_capture()
		Engine.max_fps = 30
		if _hold_pending:
			_hold_pending = false
			print("[stage0] CLICK position=", get_window().position)
			_play_action("wave")
		else:
			print("[stage0] DRAG end position=", get_window().position)
			_end_hold()


## --- Hold / fall / land -------------------------------------------------------
#
# The window follows the motion controller instead of the cursor: while held the
# character swings after the cursor with follow + damping, on release it keeps
# its velocity and falls until the soles reach the work area's bottom edge.

func _ensure_motion_bounds() -> void:
	var work := DisplayServer.screen_get_usable_rect(screen)
	_last_work_area = work
	var subject := _subject_rect()
	motion.set_bounds(work, subject.size)
	if not _motion_active and not motion.is_held() and not motion.is_falling():
		# First placement: align the controller with where the pet already is.
		var soles := Vector2(get_window().position) + Vector2(subject.get_center().x, subject.end.y)
		motion.settle_at(soles.x)


func _begin_hold(cursor: Vector2i) -> void:
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
	_settle_ground_contact(delta)
	# Hand the window back to the pinning logic only once the pet is standing on
	# the ground with the recovery done. Checking velocity alone is not enough:
	# a held pet and a just-released one both report zero velocity for a frame.
	if motion.is_grounded() and motion.is_resting() and not _recovering:
		_motion_active = false


## Re-enables foot planting after landing, but only once the pose has had time to
## come back upright; locking the ankles during the fall or recovery pose drags
## the body flat.
func _settle_ground_contact(delta: float) -> void:
	if not is_instance_valid(actor) or not actor.has_method("set_ground_contact"):
		return
	if motion.is_held() or motion.is_falling():
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
func _upright_action() -> bool:
	return current_action in ["idle", "walk", "run", "turn", "wave", "agreeing", "angry", "bashful", "bored"]


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
			_apply_actor_state()
			Engine.max_fps = 5 if hidden else 30
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
	if actor and actor.has_method("play_action"):
		actor.play_action(action)
	else:
		_on_action_changed(action)

func _on_action_changed(action: String) -> void:
	current_action = action
	print("[stage0] ACTION ", action)

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
	# While the motion controller owns the vertical position, pinning the subject
	# to an edge every frame would fight gravity and cancel the fall.
	if _motion_active:
		return
	var work := DisplayServer.screen_get_usable_rect(screen)
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
	if was_held:
		# A cancelled drag must not leave the pet hanging in mid-air.
		motion.release()
		_motion_active = true
	if bridge:
		bridge.end_capture()
	Engine.max_fps = 5 if hidden else 30

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
