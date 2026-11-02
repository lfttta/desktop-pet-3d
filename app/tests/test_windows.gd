extends SceneTree

const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
const ChatWindow = preload("res://scripts/ui/chat_window.gd")

var checks := 0
var failures := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	for window_script: Script in [SettingsWindow, ChatWindow]:
		var window: Window = window_script.new()
		window.visible = false
		root.add_child(window)
		await process_frame
		check(not window.transparent, "utility window disables native transparency")
		check(not window.transparent_bg, "utility window disables transparent viewport")
		check(window.get_child_count() > 0 and window.get_child(0) is ColorRect, "utility window has an opaque background")
		if window.get_child_count() > 0 and window.get_child(0) is ColorRect:
			check((window.get_child(0) as ColorRect).color.a == 1.0, "utility window background is fully opaque")
		window.free()
	print("WINDOW_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
