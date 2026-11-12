extends SceneTree

const SpeechBubble = preload("res://scripts/ui/speech_bubble.gd")

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
	var bubble = SpeechBubble.new()
	bubble.set_max_width(240.0)
	root.add_child(bubble)
	await process_frame
	check(not bubble.visible, "the bubble starts hidden")
	check(bubble.mouse_filter == Control.MOUSE_FILTER_IGNORE, "the bubble itself ignores the mouse")
	check(bubble.label.mouse_filter == Control.MOUSE_FILTER_IGNORE, "the bubble text ignores the mouse")
	check(bubble.label.autowrap_mode == TextServer.AUTOWRAP_WORD_SMART, "the text wraps automatically")

	check(is_equal_approx(bubble.duration_for_text("你好呀"), 4.0), "short text keeps the four second default")
	check(is_equal_approx(bubble.duration_for_text("一".repeat(21)), 5.0), "text just past twenty characters gains a second")
	check(is_equal_approx(bubble.duration_for_text("一".repeat(31)), 6.0), "each further ten characters adds a second")
	check(is_equal_approx(bubble.duration_for_text("一".repeat(200)), 10.0), "the automatic duration caps at ten seconds")

	bubble.show_bubble("你好呀")
	check(bubble.visible, "show_bubble makes the bubble visible")
	check(bubble.label.text == "你好呀", "show_bubble sets the text, got '%s'" % bubble.label.text)

	bubble.show_bubble("换一句说说")
	check(bubble.label.text == "换一句说说", "a second show replaces the text, got '%s'" % bubble.label.text)
	check(_label_count(bubble) == 1, "a second show does not stack a second label, got %d" % _label_count(bubble))

	bubble.show_bubble("马上消失", 0.15)
	check(bubble.visible, "an explicit duration still shows the bubble")
	await create_timer(0.5).timeout
	check(not bubble.visible, "the bubble hides once its duration expires")

	# A replaced bubble must survive the timer of the text it replaced.
	bubble.show_bubble("旧消息", 0.1)
	bubble.show_bubble("新消息", 0.5)
	await create_timer(0.25).timeout
	check(bubble.visible, "the stale timer of a replaced bubble does not hide the new one")
	check(bubble.label.text == "新消息", "the replacement keeps its own text")

	bubble.hide_bubble()
	check(not bubble.visible, "hide_bubble hides immediately")

	# Reply bubbles are clipped to 40 characters plus an ellipsis; a click phrase
	# passes no cap and keeps its whole text.
	bubble.set_max_chars(40)
	bubble.show_bubble("一".repeat(41))
	check(bubble.label.text == "一".repeat(40) + "…", "41 characters keep 40 and an ellipsis, got %d characters" % bubble.label.text.length())
	bubble.show_bubble("一".repeat(40))
	check(bubble.label.text == "一".repeat(40), "exactly forty characters are not clipped")
	bubble.set_max_chars(0)
	bubble.show_bubble("一".repeat(41))
	check(bubble.label.text == "一".repeat(41), "no cap keeps the whole text")
	bubble.hide_bubble()

	# Click-through stays the default; an interactive bubble takes the mouse and
	# reports one press per left click.
	check(bubble.mouse_filter == Control.MOUSE_FILTER_IGNORE, "the bubble stays click-through by default")
	bubble.set_interactive(true)
	check(bubble.mouse_filter == Control.MOUSE_FILTER_STOP, "set_interactive(true) takes the mouse")
	var presses: Array[int] = [0]
	bubble.pressed.connect(func(): presses[0] += 1)
	var press := InputEventMouseButton.new()
	press.button_index = MOUSE_BUTTON_LEFT
	press.pressed = true
	bubble.gui_input.emit(press)
	var release := InputEventMouseButton.new()
	release.button_index = MOUSE_BUTTON_LEFT
	release.pressed = false
	bubble.gui_input.emit(release)
	check(presses[0] == 1, "a left click emits pressed once, got %d" % presses[0])
	bubble.set_interactive(false)
	check(bubble.mouse_filter == Control.MOUSE_FILTER_IGNORE, "set_interactive(false) restores click-through")

	# A disabled bubble shows nothing, and an already visible one is hidden.
	bubble.show_bubble("显示中")
	bubble.enabled = false
	bubble.show_bubble("不应显示")
	check(not bubble.visible, "a disabled bubble shows nothing")
	bubble.enabled = true
	bubble.hide_bubble()

	bubble.show_bubble("这是一段很长的测试文本，".repeat(12))
	check(bubble.size.x <= 240.0, "the bubble never exceeds its maximum width, got %.1f" % bubble.size.x)
	check(bubble.label.text.length() > 20, "the width check uses long text")
	bubble.hide_bubble()
	check(not bubble.visible, "the long bubble hides too")

	print("SPEECH_BUBBLE_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _label_count(node: Node) -> int:
	var total := 0
	for child in node.get_children():
		if child is Label:
			total += 1
		total += _label_count(child)
	return total
