extends SceneTree
## End-to-end wiring check: a polled tray command must drive the real action.
##
## Runs the shipping Stage-0 PetWindow node so the path under test is the real
## one: native command number -> action_menu.action_for_command -> _handle_tray
## -> _play_action -> character_view -> AnimationPlayer -> completion.
##
## The Stage-2 main.gd wrapper is deliberately not preloaded: its _ready starts
## the Python service, which is unrelated to menu wiring and fails in headless
## test environments.

const PetWindow = preload("res://scripts/pet_window.gd")
const ActionMenu = preload("res://scripts/action_menu.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	var window := PetWindow.new()
	root.add_child(window)
	await process_frame

	var menu = window.action_menu
	var table: Dictionary = ActionMenu.load_table()
	var manifest: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/humanoid_core.json"))
	var library: Dictionary = manifest.get("actions", {}) if manifest is Dictionary else {}
	var built: Dictionary = ActionMenu.build(table, library)
	menu.configure(built.groups, built.flat)
	check(menu.actions().size() == 65, "menu exposes 65 actions")

	# Every command in range resolves to a distinct action name.
	var seen := {}
	for index in menu.actions().size():
		var command: int = ActionMenu.COMMAND_BASE + index
		var action: String = menu.action_for_command(command)
		check(not action.is_empty(), "command %d resolves" % command)
		check(not seen.has(action), "command %d is unique" % command)
		seen[action] = true
	check(seen.size() == 65, "all 65 actions are reachable by command")

	# Fixed tray commands must not be mistaken for actions.
	for fixed in [1, 2, 4, 5, 6, 7, 0]:
		check(menu.action_for_command(fixed).is_empty(), "tray command %d is not an action" % fixed)

	var view = window.actor
	check(view != null, "shipping window loaded a character")

	# The real distribution path a native menu click takes.
	var command_for := func(action: String) -> int:
		return ActionMenu.COMMAND_BASE + menu.actions().find(action)
	for action_name in ["wave", "pickup", "fall"]:
		var command: int = command_for.call(action_name)
		window._handle_tray(command)
		check(view.action == action_name, "tray command %d plays %s" % [command, action_name])
		check(view.player.current_animation == "shared/" + action_name, "tray command %d reaches AnimationPlayer" % command)

	# Menu-selected hold actions are documented as play-once: they must return
	# to idle on completion instead of freezing in the held pose.
	for action_name in ["pickup", "fall"]:
		window._handle_tray(command_for.call(action_name))
		view.player.animation_finished.emit(StringName("shared/" + action_name))
		check(view.action == "idle", "menu-selected %s returns to idle" % action_name)

	# Explicit holds (drag) must still latch until the caller releases them.
	var held: int = view.request_action("pickup", "drag", 1.0, 1, true)
	check(held > 0 and view.action == "pickup", "drag hold starts pickup")
	view.player.animation_finished.emit(StringName("shared/pickup"))
	check(view.action == "pickup", "explicit hold ignores completion")
	view.release_action(held)
	check(view.action == "idle", "release returns explicit hold to idle")

	# Every menu action must exist in the actual animation library.
	var missing: PackedStringArray = PackedStringArray()
	for action: String in menu.actions():
		if not view.player.has_animation("shared/" + action):
			missing.append(action)
	check(missing.is_empty(), "every menu action exists in godette library; missing=" + ",".join(missing))

	print("ACTION_WIRING_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)
