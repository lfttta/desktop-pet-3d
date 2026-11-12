extends PanelContainer
## Short-lived speech bubble drawn over the pet on the desktop.
##
## The bubble is presentation only: it never takes part in input, because the
## native window's hit capsule stays the one place a click can land.

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

var label: Label
var _built := false
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
	mouse_filter = Control.MOUSE_FILTER_IGNORE
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
	var body := text.strip_edges()
	_ensure_nodes()
	if body.is_empty():
		hide_bubble()
		return 0.0
	label.text = body
	_fit()
	visible = true
	var duration := duration_for_text(body) if seconds <= 0.0 else seconds
	_start_timer(duration)
	return duration

func hide_bubble() -> void:
	_generation += 1
	visible = false

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
	var natural := font.get_string_size(label.text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	var content_width := clampf(natural, 1.0, maxf(1.0, max_width - PADDING.x * 2.0))
	# The label reports its wrapped minimum height only after it knows the width
	# it has to wrap into, so the width has to be written before it is asked.
	label.size = Vector2(content_width, 0.0)
	var content_height := maxf(label.get_combined_minimum_size().y, float(label.get_line_height()))
	size = Vector2(content_width + PADDING.x * 2.0, content_height + PADDING.y * 2.0)
