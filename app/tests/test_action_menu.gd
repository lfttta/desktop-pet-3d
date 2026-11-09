extends SceneTree
## Contract tests for the action menu: table -> native payload -> command mapping.

const ActionMenu = preload("res://scripts/action_menu.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	var manifest: Variant = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/humanoid_core.json"))
	check(manifest is Dictionary, "humanoid_core manifest parses")
	var library: Dictionary = manifest.get("actions", {}) if manifest is Dictionary else {}
	check(library.size() == 65, "library exposes 65 actions")

	var table := ActionMenu.load_table()
	check(not table.is_empty(), "action_menu table parses")
	var built := ActionMenu.build(table, library)
	var flat: Array = built.flat
	check(flat.size() == 65, "menu covers every action exactly once (65), got %d" % flat.size())
	check(not built.groups.is_empty(), "menu has at least one group")

	var seen := {}
	for name: Variant in flat:
		check(not seen.has(name), "duplicate action in menu: " + str(name))
		seen[name] = true
		check(library.has(name), "menu action missing from library: " + str(name))
	for name: Variant in library.keys():
		check(seen.has(name), "library action missing from menu: " + str(name))

	var menu := ActionMenu.new()
	menu.configure(built.groups, built.flat, built.get("labels", {}))
	check(menu.actions().size() == 65, "configured menu keeps all actions")
	check(menu.group_count() == built.groups.size(), "group count preserved")

	var payload := menu.native_payload()
	var lines := payload.split("\n")
	check(lines.size() == menu.group_count(), "payload has one line per group")
	var echoed := 0
	for line in lines:
		var fields := line.split("|")
		check(fields.size() >= 2, "payload line has a label and items: " + line)
		check(not fields[0].is_empty(), "payload label is not empty")
		echoed += fields.size() - 1
		for index in range(1, fields.size()):
			# An empty field is dropped by the native parser, which would shift
			# every following command index.
			check(not fields[index].is_empty(), "payload item is not empty: " + line)
	check(echoed == 65, "payload carries 65 items, got %d" % echoed)

	# The native menu shows the library's Chinese label, not the internal name.
	check(payload.contains("待机"), "payload uses the Chinese label for idle")
	check(payload.contains("挥手"), "payload uses the Chinese label for wave")
	check(not payload.contains("|idle|") and not payload.ends_with("|idle"),
			"internal action name is not shown when a label exists")

	# Command round-trip: index i must map back to flat[i].
	for index in flat.size():
		check(menu.action_for_command(ActionMenu.COMMAND_BASE + index) == flat[index],
				"command %d maps to %s" % [ActionMenu.COMMAND_BASE + index, flat[index]])
	check(menu.action_for_command(ActionMenu.COMMAND_BASE - 1).is_empty(), "index below base is not an action")
	check(menu.action_for_command(ActionMenu.COMMAND_BASE + 65).is_empty(), "index past the end is not an action")
	check(menu.action_for_command(4).is_empty(), "fixed tray command is not an action")

	# Unknown or duplicated names never reach the native menu.
	var sanitized := ActionMenu.build(
			{"categories": [
				{"label": "A", "actions": ["wave", "not_a_real_action"]},
				{"label": "B", "actions": ["wave", "idle"]},
			]},
			library)
	check(sanitized.flat == ["wave", "idle"], "unknown and duplicate actions are dropped")
	var empty_groups := ActionMenu.build({"categories": [{"label": "Z", "actions": ["nope"]}]}, library)
	check(empty_groups.groups.is_empty(), "group with no valid actions is dropped")

	print("ACTION_MENU_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)
