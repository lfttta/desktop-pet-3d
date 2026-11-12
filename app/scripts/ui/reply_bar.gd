extends Window
## A small input bar that opens where the speech bubble was, so one line can be
## typed without opening the full chat window.
##
## It is a native subwindow on purpose. The pet window is created with
## `no_focus`; on Windows that style cannot be turned off at runtime, so a
## LineEdit inside the bubble would never receive a keystroke. A separate
## focused window gets the keyboard the same way the chat window does.

## Emitted when the typed line is ready. The text is trimmed and never empty.
signal submitted(text: String)
## Emitted on Escape or when the bar loses focus.
signal cancelled
## Emitted by the "聊天" button: open the full chat window.
signal chat_requested

const BACKGROUND := Color("1c2532")
const BAR_SIZE := Vector2i(380, 58)
const PADDING := 8

var entry: LineEdit
var chat_button: Button
var _built := false


func _init() -> void:
	title = "Pet3D 回复"
	borderless = true
	always_on_top = true
	transparent_bg = false
	unfocusable = false
	size = BAR_SIZE
	visible = false
	# Losing focus is a normal way to dismiss the bar; the pet must not keep a
	# floating input around while another application is being used.
	focus_exited.connect(_on_focus_exited)


func _ready() -> void:
	_ensure_nodes()


func _ensure_nodes() -> void:
	if _built:
		return
	_built = true
	var panel := PanelContainer.new()
	panel.set_anchors_preset(Control.PRESET_FULL_RECT)
	var style := StyleBoxFlat.new()
	style.bg_color = BACKGROUND
	style.set_corner_radius_all(10)
	style.content_margin_left = PADDING
	style.content_margin_right = PADDING
	style.content_margin_top = PADDING
	style.content_margin_bottom = PADDING
	panel.add_theme_stylebox_override("panel", style)
	add_child(panel)
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	panel.add_child(row)
	entry = LineEdit.new()
	entry.placeholder_text = "回一句…"
	entry.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	entry.text_submitted.connect(_on_submitted)
	entry.gui_input.connect(_on_entry_gui_input)
	row.add_child(entry)
	chat_button = Button.new()
	chat_button.text = "聊天"
	chat_button.pressed.connect(_on_chat_pressed)
	row.add_child(chat_button)


## Shows the bar at `screen_position` (the bubble's top-left in screen pixels)
## and gives the field the caret.
func open_at(screen_position: Vector2i) -> void:
	_ensure_nodes()
	position = screen_position
	entry.text = ""
	show()
	grab_focus()
	entry.grab_focus.call_deferred()


func close() -> void:
	visible = false


func is_open() -> bool:
	return visible


func _on_submitted(text: String) -> void:
	var body := text.strip_edges()
	if body.is_empty():
		return
	close()
	submitted.emit(body)


func _on_entry_gui_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key != null and key.pressed and key.keycode == KEY_ESCAPE:
		close()
		cancelled.emit()


func _on_chat_pressed() -> void:
	close()
	chat_requested.emit()


func _on_focus_exited() -> void:
	if not visible:
		return
	close()
	cancelled.emit()
