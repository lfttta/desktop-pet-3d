extends Window
signal retry_requested
var status: Label
func _ready() -> void:
	title = "Pet3D 聊天"
	size = Vector2i(520, 360)
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
		margin.add_theme_constant_override("margin_" + side, 24)
	add_child(margin)
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 18)
	margin.add_child(column)
	var title_label := Label.new()
	title_label.text = "陪伴，从稳定运行开始"
	title_label.add_theme_font_size_override("font_size", 23)
	column.add_child(title_label)
	var explanation := Label.new()
	explanation.text = "当前版本已支持角色与桌面设置保存。\n聊天、人格和记忆会在后续阶段接入。"
	explanation.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(explanation)
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(status)
	var retry := Button.new()
	retry.text = "重新连接后台服务"
	retry.pressed.connect(func(): retry_requested.emit())
	column.add_child(retry)
	var close := Button.new()
	close.text = "关闭"
	close.pressed.connect(hide)
	column.add_child(close)
func show_status(detail: String) -> void:
	status.text = detail
func open() -> void:
	show()
	grab_focus()
