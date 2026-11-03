extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var catalog: Array = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	var window: Window = load("res://scripts/ui/settings_window.gd").new()
	window.visible = false
	root.add_child(window)
	await process_frame
	window.populate({"user_name": "Test", "scale": 1.0, "screen_id": ""}, [])
	var failures := 0
	if catalog.size() != 1 or catalog[0].id != "godette":
		failures += 1
	# Only the display selector remains.
	if window.find_children("*", "OptionButton", true, false).size() != 1:
		failures += 1
	var patches: Array = []
	window.save_requested.connect(func(value): patches.append(value))
	window._save()
	if patches.size() != 1 or patches[0].has("character_id"):
		failures += 1
	window.free()
	print("STAGE2_CATALOG_TESTS checks=3 failures=", failures)
	quit(0 if failures == 0 else 1)
