class_name ActionMenu
extends RefCounted
## Action menu model: category table -> native menu payload -> command index.
##
## The native tray menu owns the popup; this class owns the authoritative order.
## Native echoes action_command_base + flat index, so the same flat list built
## here is what maps a command back to an action name.

const COMMAND_BASE := 1000

var _flat: Array[String] = []
var _labels: Dictionary = {}


static func load_table(path := "res://assets_data/action_menu.json") -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if parsed is Dictionary:
		return parsed
	return {}


## Build from the menu table plus the runtime action library.
## Returns {groups: [[label, [action names]]], flat: [action names]}.
## Actions missing from the library are dropped; unknown ones never reach native.
static func build(table: Dictionary, library: Dictionary) -> Dictionary:
	var groups: Array = []
	var flat: Array[String] = []
	var categories: Variant = table.get("categories", [])
	if not (categories is Array):
		return {"groups": groups, "flat": flat}
	for entry: Variant in categories:
		if not (entry is Dictionary):
			continue
		var label := str(entry.get("label", ""))
		var listed: Variant = entry.get("actions", [])
		if label.is_empty() or not (listed is Array):
			continue
		var names: Array[String] = []
		for name: Variant in listed:
			var action := str(name)
			if library.has(action) and not flat.has(action):
				names.append(action)
				flat.append(action)
		if not names.is_empty():
			groups.append([label, names])
	return {"groups": groups, "flat": flat}


func configure(groups: Array, flat: Array) -> void:
	_flat.clear()
	_labels.clear()
	for name: String in flat:
		_flat.append(name)
	for entry: Array in groups:
		if entry.size() != 2:
			continue
		var names: Array = entry[1]
		for name: Variant in names:
			_labels[str(name)] = str(entry[0])


## One line per group: "label|action|action". Labels and names are display text.
func native_payload() -> String:
	var lines: PackedStringArray = PackedStringArray()
	for label: String in _ordered_labels():
		var items: PackedStringArray = PackedStringArray()
		for action: String in _flat:
			if str(_labels.get(action, "")) == label:
				items.append(action)
		if not items.is_empty():
			lines.append(label + "|" + "|".join(items))
	return "\n".join(lines)


## Map a polled tray command to an action name, or "" when it is not an action.
func action_for_command(command: int) -> String:
	var index := command - COMMAND_BASE
	if index < 0 or index >= _flat.size():
		return ""
	return _flat[index]


func actions() -> Array[String]:
	return _flat.duplicate()


func group_count() -> int:
	return _ordered_labels().size()


func _ordered_labels() -> Array[String]:
	var seen: Array[String] = []
	for action: String in _flat:
		var label := str(_labels.get(action, ""))
		if not label.is_empty() and not seen.has(label):
			seen.append(label)
	return seen
