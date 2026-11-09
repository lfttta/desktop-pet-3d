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
## Internal action name -> text shown in the native menu.
var _display: Dictionary = {}


static func load_table(path := "res://assets_data/action_menu.json") -> Dictionary:
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if parsed is Dictionary:
		return parsed
	return {}


## Build from the menu table plus the runtime action library.
## Returns {groups: [[label, [action names]]], flat: [action names],
##          labels: {action name: display label}}.
## Actions missing from the library are dropped; unknown ones never reach native.
## The display label comes from the library entry, so the native menu can show
## Chinese text while the flat order stays keyed by the internal action name.
static func build(table: Dictionary, library: Dictionary) -> Dictionary:
	var groups: Array = []
	var flat: Array[String] = []
	var labels: Dictionary = {}
	var categories: Variant = table.get("categories", [])
	if not (categories is Array):
		return {"groups": groups, "flat": flat, "labels": labels}
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
				var info: Variant = library[action]
				var shown := str(info.get("label", "")) if info is Dictionary else ""
				labels[action] = action if shown.is_empty() else shown
		if not names.is_empty():
			groups.append([label, names])
	return {"groups": groups, "flat": flat, "labels": labels}


func configure(groups: Array, flat: Array, labels := {}) -> void:
	_flat.clear()
	_labels.clear()
	_display.clear()
	for name: String in flat:
		_flat.append(name)
	for entry: Array in groups:
		if entry.size() != 2:
			continue
		var names: Array = entry[1]
		for name: Variant in names:
			_labels[str(name)] = str(entry[0])
	for name: Variant in labels:
		_display[str(name)] = str(labels[name])


## One line per group: "label|item|item". Items are the display text the native
## menu shows; the command index maps back to the flat position, so the text is
## free-form. It is the action's Chinese label, falling back to the internal name
## when the library has none, because the native parser drops empty fields and a
## dropped item would shift every later index.
func native_payload() -> String:
	var lines: PackedStringArray = PackedStringArray()
	for label: String in _ordered_labels():
		var items: PackedStringArray = PackedStringArray()
		for action: String in _flat:
			if str(_labels.get(action, "")) != label:
				continue
			var shown := str(_display.get(action, ""))
			items.append(action if shown.is_empty() else shown)
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
