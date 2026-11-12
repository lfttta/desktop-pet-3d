extends SceneTree
## Reply bar opened by clicking the desktop bubble: it must be a separate,
## focusable window (the pet window cannot take the keyboard), trim what is
## typed, close on Escape / focus loss, and hand the line to the app layer.

const ReplyBar = preload("res://scripts/ui/reply_bar.gd")
const PetWindow = preload("res://scripts/pet_window.gd")

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
