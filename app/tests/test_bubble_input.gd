extends SceneTree
## Reply bar opened by clicking the desktop bubble: it must be a separate,
## focusable window (the pet window cannot take the keyboard), trim what is
## typed, close on Escape / focus loss, and hand the line to the app layer.

const ReplyBar = preload("res://scripts/ui/reply_bar.gd")
const PetWindow = preload("res://scripts/pet_window.gd")
const FakeBridge = preload("res://tests/fake_bridge.gd")

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
	await _test_bar()
	await _test_window_handoff()
	await _test_bar_against_pet()
	print("BUBBLE_INPUT_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)


func _test_bar() -> void:
	var bar := ReplyBar.new()
	root.add_child(bar)
	await process_frame
	var lines: Array = []
	var cancels := [0]
	var chats := [0]
	bar.submitted.connect(func(text: String): lines.append(text))
	bar.cancelled.connect(func(): cancels[0] += 1)
	bar.chat_requested.connect(func(): chats[0] += 1)

	check(not bar.is_open(), "the bar starts hidden")
	check(bar.borderless and bar.always_on_top, "the bar is borderless and stays on top")
	check(not bar.unfocusable, "the bar can take the keyboard")
	check(bar.entry != null, "the bar has a field")

	bar.open_at(Vector2i(100, 200))
	await process_frame
	check(bar.is_open(), "open_at shows the bar")
	check(bar.position == Vector2i(100, 200), "open_at uses the bubble's spot")
	check(bar.gui_get_focus_owner() == bar.entry, "the field takes the caret, got %s" % [bar.gui_get_focus_owner()])

	bar.entry.text_submitted.emit("  晚上好  ")
	check(lines == ["晚上好"], "a submitted line is trimmed, got %s" % [lines])
	check(not bar.is_open(), "submitting closes the bar")

	bar.open_at(Vector2i(100, 200))
	bar.entry.text_submitted.emit("   ")
	check(lines.size() == 1 and bar.is_open(), "a blank submit is ignored and keeps the bar")

	var escape := InputEventKey.new()
	escape.keycode = KEY_ESCAPE
	escape.pressed = true
	bar.entry.gui_input.emit(escape)
	check(cancels[0] == 1, "escape cancels")
	check(not bar.is_open(), "escape closes the bar")

	bar.open_at(Vector2i(100, 200))
	bar.chat_button.pressed.emit()
	check(chats[0] == 1, "the chat button asks for the chat window")
	check(not bar.is_open(), "the chat button closes the bar")

	bar.open_at(Vector2i(100, 200))
	bar.focus_exited.emit()
	check(cancels[0] == 2, "losing focus cancels")
	check(not bar.is_open(), "losing focus closes the bar")
	bar.free()


func _test_window_handoff() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	var replies: Array = []
	var chats := [0]
	pet.bubble_reply_submitted.connect(func(text: String): replies.append(text))
	pet.bubble_chat_requested.connect(func(): chats[0] += 1)

	pet.show_bubble("在的")
	pet._on_bubble_pressed()
	await process_frame
	check(is_instance_valid(pet.reply_bar), "a bubble click creates the reply bar")
	check(pet.reply_bar.is_open(), "a bubble click opens the reply bar")
	check(not pet.speech_bubble.visible, "the bubble gives way to the bar")
	check(pet.reply_bar.position.x >= 0 and pet.reply_bar.position.y >= 0,
		"the bar opens inside the work area, got %s" % [pet.reply_bar.position])

	pet.reply_bar.entry.text_submitted.emit("  说句话  ")
	check(replies == ["说句话"], "the typed line reaches the app layer, got %s" % [replies])
	check(not pet.reply_bar.is_open(), "submitting closes the bar")

	pet.show_bubble("再说一句")
	pet._on_bubble_pressed()
	pet.reply_bar.chat_button.pressed.emit()
	check(chats[0] == 1, "the bar's chat button reaches the app layer")

	pet.show_bubble("第三句")
	pet._on_bubble_pressed()
	pet.set_bubble_enabled(false)
	check(not pet.reply_bar.is_open(), "turning bubbles off closes the bar")
	pet.free()


## The bar must not float over a pet that keeps moving: clicking the bubble
## stops a walk, the behaviour chain stays quiet while the bar is open, and a
## press on the pet closes the bar.
func _test_bar_against_pet() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	pet.work_area_override = Rect2i(0, 0, 1920, 1080)
	pet._ensure_motion_bounds()
	pet.motion.settle_at(960.0)
	pet._play_action("idle")

	check(pet._begin_walk(1, 1), "the pet can walk for the test")
	pet.show_bubble("在的")
	pet._on_bubble_pressed()
	check(not pet._walk_active, "a bubble click stops the walk")
	check(pet.reply_bar.is_open(), "the bar still opens after stopping the walk")

	pet.close_reply_bar()
	pet._play_action("idle")
	pet.behavior_enabled = true
	pet._behavior_cooldown = 0.0
	pet.behavior.roll_override = func(): return 0.99
	pet.reply_bar.open_at(Vector2i(10, 10))
	pet._update_behavior(1.0 / 60.0)
	check(pet._behavior_cooldown == 0.0, "no decision while the reply bar is open")
	pet.close_reply_bar()
	pet._update_behavior(1.0 / 60.0)
	check(pet._behavior_cooldown > 0.0, "decisions resume after the bar closes")

	# Drive the real input loop with a stand-in bridge: a press on the pet closes
	# the bar before the click/drag branch runs.
	var fake := FakeBridge.new()
	pet.bridge = fake
	pet.drag.cancel()
	pet._motion_active = false
	pet._hold_pending = false
	pet.reply_bar.open_at(Vector2i(10, 10))
	pet.work_area_override = Rect2i(0, 0, 1920, 1000)
	pet.regions = [{"a": Vector2(0, 0), "b": Vector2(400, 400), "radius": 400.0}]
	fake.cursor = root.position + Vector2i(30, 30)
	fake.down = false
	pet._update_input()
	fake.down = true
	pet._update_input()
	check(not pet.reply_bar.is_open(), "a press on the pet closes the reply bar")
	fake.down = false
	pet._update_input()

	# Hiding the pet from the tray must not leave a focused bar on the desktop.
	pet.reply_bar.open_at(Vector2i(10, 10))
	pet._handle_tray(1)
	check(not pet.reply_bar.is_open(), "hiding the pet closes the reply bar")
	pet._handle_tray(1)
	pet.free()
