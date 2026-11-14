extends SceneTree
const Settings = preload("res://scripts/app_settings.gd")
const ScreenMath = preload("res://scripts/screen_math.gd")
func _initialize() -> void:
	var work := Rect2i(-1920, 0, 1920, 1040)
	var subject := Rect2(200, 100, 200, 600)
	var saved := Settings.placement(Vector2i(-1200, 200), subject, work, Vector2i.ZERO)
	assert(is_equal_approx(saved.x, 0.53125) and is_equal_approx(saved.y, 900.0 / 1040.0))
	assert(Settings.position_from(saved, subject, work) == Vector2i(-1200, 200))
	var bigger := Rect2(250, 125, 250, 750)
	var restored := Settings.position_from(saved, bigger, work)
	assert(absf(restored.y + bigger.end.y - 900) <= 1)
	# Utility windows never exceed the primary work area; a small screen shrinks
	# them instead of hanging controls off the desktop.
	assert(Settings.fit_window_size(Vector2i(720, 560), Rect2i(0, 0, 1024, 768), 16) == Vector2i(720, 560))
	assert(Settings.fit_window_size(Vector2i(720, 560), Rect2i(0, 0, 640, 480), 16) == Vector2i(608, 448))
	assert(Settings.fit_window_size(Vector2i(520, 400), Rect2i(0, 0, 400, 300), 0) == Vector2i(400, 300))
	var edges := Vector2i(1, 1)
	var pinned := ScreenMath.pin_edges(Settings.position_from(saved, subject, work), subject, work, edges)
	assert(pinned + Vector2i(subject.end) == work.end)
	assert(Settings.utility_position(Vector2i(840, 660), Rect2i(0, 0, 2560, 1528)) == Vector2i(860, 434))
	print("APP_SETTINGS_TESTS checks=8 failures=0")
	quit(0)
