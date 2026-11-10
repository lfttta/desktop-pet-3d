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

	_test_chat_actions(window, view)

	print("ACTION_WIRING_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)


## A chat reply may name an animation, but only one this build can play safely.
##
## The service validates against the same library; these checks are the app's own
## guard against a stale service, a newer build, or a reply naming a clip the
## chat path must not drive (a loop or a hold would never hand control back).
func _test_chat_actions(window, view) -> void:
	window._play_action("idle")
	window._play_chat_action("dancing")
	check(view.action == "dancing", "a chat action plays, got %s" % view.action)
	check(view.player.current_animation == "shared/dancing", "a chat action reaches the AnimationPlayer")

	# Ids the chat path must ignore: unknown, empty, explicit none, loop-only and
	# hold-only clips.
	for rejected in ["", "none", "does_not_exist", "running", "sleeping_idle", "pickup", "fall"]:
		window._play_action("idle")
		window._play_chat_action(rejected)
		check(view.action == "idle", "'%s' from chat is ignored, got %s" % [rejected, view.action])

	# Chat sits below the user in the priority table: a reply arriving while the
	# user's own action is playing must not cut it short.
	window._play_action("idle")
	window._handle_tray(ActionMenu.COMMAND_BASE + window.action_menu.actions().find("wave"))
	check(view.action == "wave", "the user action starts before the chat request")
	window._play_chat_action("crying")
	check(view.action == "wave", "a chat action does not override a user action, got %s" % view.action)
	window._play_action("idle")
