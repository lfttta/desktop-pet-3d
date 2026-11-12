extends PanelContainer
## Short-lived speech bubble drawn over the pet on the desktop.
##
## The bubble stays click-through by default, so it never blocks the desktop.
## A caller that wants a bubble click to do something switches it on with
## `set_interactive(true)`; the native window then includes the bubble rectangle
## in its hit area and the click arrives here as `pressed`.

## Emitted when a left click started and ended on an interactive bubble.
signal pressed

const DEFAULT_SECONDS := 4.0
const MAX_SECONDS := 10.0
## Text up to this length keeps the default duration.
const LONG_TEXT_LENGTH := 20
const EXTRA_STEP_LENGTH := 10
const SECONDS_PER_STEP := 1.0
const PADDING := Vector2(14.0, 10.0)
const BACKGROUND := Color("1c2532")
const CORNER_RADIUS := 12
const FONT_SIZE := 16

## Widest the whole bubble may become. The pet window narrows this to its own
## width so the text never spills past the window edge.
var max_width := 320.0

## When false `show_bubble` refuses to display anything. The pet window mirrors
## the service's bubble.enabled setting here.
var enabled := true
## Longest text shown, in characters; zero or less leaves it uncapped. A clipped
## text ends with an ellipsis so the shortened line reads as incomplete.
var max_chars := 0
## Whether the bubble takes mouse input. Off by default: an ordinary bubble is
## presentation only and must not swallow clicks meant for the desktop.
var interactive := false

var label: Label
var _built := false
## True between a left press and its release inside the bubble.
var _pressed_inside := false
## Invalidates pending timers: a stale timeout must not hide a newer bubble.
var _generation := 0

func _init() -> void:
	# Set before the node ever enters a tree: the bubble must never look
	# clickable, even for the one frame before `_ready` builds its contents.
	mouse_filter = Control.MOUSE_FILTER_IGNORE

func _ready() -> void:
	_ensure_nodes()

## Builds the style and label on first use, so a caller may show text before the
## node has entered the tree.
func _ensure_nodes() -> void:
	if _built:
		return
	_built = true
	visible = false
	_apply_mouse_filter()
	gui_input.connect(_on_gui_input)
	var style := StyleBoxFlat.new()
	style.bg_color = BACKGROUND
	style.set_corner_radius_all(CORNER_RADIUS)
	style.content_margin_left = PADDING.x
	style.content_margin_right = PADDING.x
	style.content_margin_top = PADDING.y
	style.content_margin_bottom = PADDING.y
	add_theme_stylebox_override("panel", style)
	label = Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.add_theme_font_size_override("font_size", FONT_SIZE)
	label.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(label)
	_fit()

## Shows `text`, replacing whatever was displayed, and returns the seconds used.
## `seconds` below zero picks the automatic duration from the text length.
func show_bubble(text: String, seconds := -1.0) -> float:
	var body := _clip(text.strip_edges())
	if body.is_empty() or not enabled:
		hide_bubble()
		return 0.0
	_ensure_nodes()
	# An autowrapping label reports its minimum height for the width it already
	# has, so give it the final width before handing it the text. Setting the
	# text first makes it report one line per character and inflates the panel
	# far past the window.
	label.size = Vector2(_content_width(body), label.size.y)
	label.text = body
	_fit()
	visible = true
	var duration := duration_for_text(body) if seconds <= 0.0 else seconds
	_start_timer(duration)
	return duration

func hide_bubble() -> void:
	_generation += 1
	_pressed_inside = false
	visible = false

## Caps the text shown, in characters. Zero or less shows the whole text, which
## is what a click phrase wants; reply bubbles pass a limit.
func set_max_chars(value: int) -> void:
	max_chars = value

## Lets the bubble take the mouse. Enabled sets MOUSE_FILTER_STOP and reports
## clicks through `pressed`; disabled restores the default click-through.
func set_interactive(value: bool) -> void:
	interactive = value
	_apply_mouse_filter()

func _apply_mouse_filter() -> void:
	mouse_filter = Control.MOUSE_FILTER_STOP if interactive else Control.MOUSE_FILTER_IGNORE

## Prunes the text to `max_chars` and marks it with an ellipsis so a reader can
## tell the line was shortened.
func _clip(text: String) -> String:
	if max_chars <= 0 or text.length() <= max_chars:
		return text
	return text.substr(0, max_chars) + "…"

## Reports a click once, on release, and only when the press began on the bubble.
func _on_gui_input(event: InputEvent) -> void:
	if not interactive:
		return
	var button := event as InputEventMouseButton
	if button == null or button.button_index != MOUSE_BUTTON_LEFT:
		return
	if button.pressed:
		_pressed_inside = true
		accept_event()
		return
	if _pressed_inside:
		_pressed_inside = false
		pressed.emit()
		accept_event()

## Changes the width limit, refitting the visible bubble to it. The limit is not
## floored above the caller's value: the window must be able to keep the bubble
## inside itself.
func set_max_width(value: float) -> void:
	var clamped := maxf(1.0, value)
	if is_equal_approx(clamped, max_width):
		return
	max_width = clamped
	if _built and visible:
		_fit()

func get_bubble_size() -> Vector2:
	return size

## Default 4 s, plus a second for every started group of ten characters past
## 20, capped at 10 s. Partial groups count so the moment a text crosses the
## threshold it gains the extra time it needs.
func duration_for_text(text: String) -> float:
	var length := text.strip_edges().length()
	if length <= LONG_TEXT_LENGTH:
		return DEFAULT_SECONDS
	var extra := ceili(float(length - LONG_TEXT_LENGTH) / float(EXTRA_STEP_LENGTH))
	return minf(MAX_SECONDS, DEFAULT_SECONDS + float(extra) * SECONDS_PER_STEP)

func _start_timer(seconds: float) -> void:
	_generation += 1
	if not is_inside_tree():
		return
	var generation := _generation
	get_tree().create_timer(maxf(0.01, seconds)).timeout.connect(_expire.bind(generation))

func _expire(generation: int) -> void:
	if generation != _generation:
		return
	hide_bubble()

## Shrink-wraps the panel around the wrapped text, up to `max_width`.
func _fit() -> void:
	if label == null:
		return
	var font := label.get_theme_font("font")
	var font_size := label.get_theme_font_size("font_size")
	var content_width := _content_width(label.text)
	# Let the label wrap at that width before the height is measured.
	label.size = Vector2(content_width, label.size.y)
	var content_height := maxf(
		font.get_multiline_string_size(label.text, HORIZONTAL_ALIGNMENT_LEFT, content_width, font_size).y,
		float(label.get_line_height()))
	label.size = Vector2(content_width, content_height)
	size = Vector2(content_width + PADDING.x * 2.0, content_height + PADDING.y * 2.0)

## Width the label is allowed to take: its unwrapped text, capped by `max_width`
## minus the panel padding.
func _content_width(text: String) -> float:
	var font := label.get_theme_font("font")
	var font_size := label.get_theme_font_size("font_size")
	var natural := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	return clampf(natural, 1.0, maxf(1.0, max_width - PADDING.x * 2.0))
