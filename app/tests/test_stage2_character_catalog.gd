extends SceneTree

const SettingsWindow = preload("res://scripts/ui/settings_window.gd")


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	var catalog: Array = JSON.parse_string(FileAccess.get_file_as_string("res://assets_data/characters.json"))
	var window := SettingsWindow.new()
	root.add_child(window)
	await process_frame
	window.configure_characters(catalog)
	window.populate({"user_name": "你", "character_id": "godette", "scale": 1.0, "screen_id": ""}, [])
	assert(window.character_choice.item_count == 1)
	assert(window.selected_character_id() == "godette")
	assert(window.character_choice.get_item_text(0).contains("Godette"))
	window.free()
	print("STAGE2_CATALOG_TESTS checks=3 failures=0")
	quit(0)
