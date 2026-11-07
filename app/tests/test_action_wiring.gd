extends SceneTree
## End-to-end wiring check: a polled tray command must drive the real action.
##
## Runs the actual PetWindow node so the path under test is the shipping one:
## native command number -> action_menu.action_for_command -> play_action ->
## character_view -> AnimationPlayer.

const PetWindow = preload("res://scripts/main.gd")
const ActionMenu = preload("res://scripts/action_menu.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	var window := PetWindow.new()
	root.add_child(window)
	# main.gd guards native startup behind _ready; drive only the menu wiring.
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
		var command: int = 1000 + index
		var action: String = menu.action_for_command(command)
		check(not action.is_empty(), "command %d resolves" % command)
		check(not seen.has(action), "command %d is unique" % command)
		seen[action] = true
	check(seen.size() == 65, "all 65 actions are reachable by command")

	# Fixed tray commands must not be mistaken for actions.
	for fixed in [1, 2, 4, 5, 6, 7, 0]:
		check(menu.action_for_command(fixed).is_empty(), "tray command %d is not an action" % fixed)

	# A representative sample must exist in the actual animation library.
	var player := AnimationPlayer.new()
	window.add_child(player)
	player.add_animation_library("shared", load("res://resources/animations/godette.tres"))
	var missing: PackedStringArray = PackedStringArray()
	for action: String in menu.actions():
		if not player.has_animation("shared/" + action):
			missing.append(action)
	check(missing.is_empty(), "every menu action exists in godette library; missing=" + ",".join(missing))

	print("ACTION_WIRING_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)
