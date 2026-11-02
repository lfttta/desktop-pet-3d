extends Window

signal save_requested(patch: Dictionary)
signal recover_requested
signal retry_requested

var name_input: LineEdit
var character_choice: OptionButton
var display_choice: OptionButton
var size_slider: HSlider
var size_value: Label
var status: Label
var message: Label
var save_button: Button
var retry_button: Button
var _display_ids: Array[String] = []
var _character_ids: Array[String] = []

func _ready() -> void:
	title = "Pet3D 设置"
	size = Vector2i(560, 440)
	min_size = size
	content_scale_size = size
	content_scale_mode = Window.CONTENT_SCALE_MODE_CANVAS_ITEMS
	unfocusable = false
	transparent = false
	transparent_bg = false
	close_requested.connect(hide)
	var background := ColorRect.new()
	background.color = Color("1c2532")
	background.mouse_filter = Control.MOUSE_FILTER_IGNORE
	add_child(background)
	background.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	var margin := MarginContainer.new()
	margin.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)
	for side in ["left", "right", "top", "bottom"]:
		margin.add_theme_constant_override("margin_" + side, 22)
	add_child(margin)
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 14)
	margin.add_child(column)
	var heading := Label.new()
	heading.text = "让桌宠适合你的桌面"
	heading.add_theme_font_size_override("font_size", 24)
	column.add_child(heading)
	name_input = LineEdit.new()
	name_input.max_length = 80
	_row(column, "称呼", name_input)
	character_choice = OptionButton.new()
	_row(column, "角色", character_choice)
	display_choice = OptionButton.new()
	_row(column, "显示器", display_choice)
	var slider_row := HBoxContainer.new()
	size_slider = HSlider.new()
	size_slider.min_value = 0.5
	size_slider.max_value = 2.0
	size_slider.step = 0.05
	size_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_value = Label.new()
	size_value.custom_minimum_size.x = 64
	slider_row.add_child(size_slider)
	slider_row.add_child(size_value)
	size_slider.value_changed.connect(func(value: float): size_value.text = "%d%%" % roundi(value * 100))
	_row(column, "显示大小", slider_row)
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(status)
	message = Label.new()
	message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	message.custom_minimum_size.y = 44
	column.add_child(message)
	var buttons := HBoxContainer.new()
	column.add_child(buttons)
	save_button = Button.new()
	save_button.text = "保存设置"
	save_button.pressed.connect(_save)
	buttons.add_child(save_button)
	var recover := Button.new()
	recover.text = "找回角色"
	recover.pressed.connect(func(): recover_requested.emit())
	buttons.add_child(recover)
	retry_button = Button.new()
	retry_button.text = "重新连接"
	retry_button.pressed.connect(func(): retry_requested.emit())
	buttons.add_child(retry_button)
	var close := Button.new()
	close.text = "关闭"
	close.pressed.connect(hide)
	buttons.add_child(close)

func _row(column: VBoxContainer, text: String, control: Control) -> void:
	var row := HBoxContainer.new()
	var label := Label.new()
	label.text = text
	label.custom_minimum_size.x = 100
	row.add_child(label)
	control.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(control)
	column.add_child(row)

func populate(settings: Dictionary, displays: Array) -> void:
	name_input.text = str(settings.get("user_name", "你"))
	var character_id := str(settings.get("character_id", "godette"))
	character_choice.select(maxi(0, _character_ids.find(character_id)))
	size_slider.value = float(settings.get("scale", 1))
	size_value.text = "%d%%" % roundi(size_slider.value * 100)
	display_choice.clear()
	_display_ids.clear()
	display_choice.add_item("主显示器（自动）")
	_display_ids.append("")
	var wanted := str(settings.get("screen_id", ""))
	var selected := 0
	for display: Dictionary in displays:
		display_choice.add_item(str(display.get("name", display.id)))
		_display_ids.append(str(display.id))
		if str(display.id) == wanted:
			selected = _display_ids.size() - 1
	if not wanted.is_empty() and selected == 0:
		display_choice.add_item("已保存的显示器（暂时未连接）")
		_display_ids.append(wanted)
		selected = _display_ids.size() - 1
	display_choice.select(selected)

func _save() -> void:
	var character_id := selected_character_id()
	save_requested.emit({"user_name": name_input.text, "character_id": character_id, "scale": size_slider.value, "screen_id": _display_ids[display_choice.selected]})


func configure_characters(catalog: Array) -> void:
	character_choice.clear()
	_character_ids.clear()
	for entry: Dictionary in catalog:
		character_choice.add_item(str(entry.get("name", entry.get("id", ""))))
		_character_ids.append(str(entry.get("id", "")))


func selected_character_id() -> String:
	if character_choice.selected >= 0 and character_choice.selected < _character_ids.size():
		return _character_ids[character_choice.selected]
	return "godette"

func show_status(state: String, detail: String) -> void:
	status.text = detail
	save_button.disabled = state != "ready"
	retry_button.disabled = state in ["ready", "starting", "restarting"]

func show_message(text: String, failed := false) -> void:
	message.text = text
	message.modulate = Color(1, 0.55, 0.5) if failed else Color(0.65, 0.9, 0.7)

func open() -> void:
	show()
	grab_focus()
