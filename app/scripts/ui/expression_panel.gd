extends VBoxContainer
## Expression style controls and phrase switches. Style values are saved with
## the regular settings patch; phrase commands stay disabled when the service
## does not advertise or implement the expression capability.

const UNAVAILABLE_TEXT := "短语功能未接入。"
const STYLE_FIELDS := [
	{"key": "casualness", "label": "口语"},
	{"key": "warmth", "label": "温暖"},
	{"key": "directness", "label": "直接"},
	{"key": "playfulness", "label": "俏皮"},
	{"key": "sentence_length", "label": "句长"},
	{"key": "emoji_frequency", "label": "表情"},
]
const STYLE_DEFAULT := 0.5

var sliders := {}
var value_labels := {}
var preset_status: Label
var phrases_status: Label
var phrases_box: VBoxContainer
var refresh_button: Button
var phrase_checks: Array = []
var _service
var _requests := {}
var _service_state := "stopped"
var _available := false

func _ready() -> void:
	add_theme_constant_override("separation", 8)
	_section("表达风格")
	var hint := Label.new()
	hint.text = "数值越高，回复越偏向对应的风格。保存设置后生效。"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(hint)
	for field in STYLE_FIELDS:
		_add_style_row(str(field.key), str(field.label))
	var preset_row := HBoxContainer.new()
	preset_row.add_theme_constant_override("separation", 6)
	var customer := Button.new()
	customer.text = "太像客服"
	customer.pressed.connect(func(): _apply_preset("customer"))
	preset_row.add_child(customer)
	var noisy := Button.new()
	noisy.text = "太吵"
	noisy.pressed.connect(func(): _apply_preset("noisy"))
	preset_row.add_child(noisy)
	var reset := Button.new()
	reset.text = "恢复默认"
	reset.pressed.connect(func(): _apply_preset("default"))
	preset_row.add_child(reset)
	add_child(preset_row)
	preset_status = Label.new()
	preset_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(preset_status)
	_section("短语开关")
	var header := HBoxContainer.new()
	add_child(header)
	phrases_status = Label.new()
	phrases_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	phrases_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(phrases_status)
	refresh_button = Button.new()
	refresh_button.text = "刷新短语"
	refresh_button.pressed.connect(refresh_phrases)
	header.add_child(refresh_button)
	var scroll := ScrollContainer.new()
	scroll.custom_minimum_size = Vector2(0, 140)
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(scroll)
	phrases_box = VBoxContainer.new()
	phrases_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(phrases_box)
	phrases_status.text = "后台服务未连接。"
	_refresh_controls()

func _section(text: String) -> void:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", 17)
	add_child(label)

func _add_style_row(key: String, label_text: String) -> void:
	var row := HBoxContainer.new()
	var label := Label.new()
	label.text = label_text
	label.custom_minimum_size.x = 64
	row.add_child(label)
	var slider := HSlider.new()
	slider.min_value = 0.0
	slider.max_value = 1.0
	slider.step = 0.05
	slider.value = STYLE_DEFAULT
	slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(slider)
	var value := Label.new()
	value.custom_minimum_size.x = 40
	value.text = "%.2f" % STYLE_DEFAULT
	row.add_child(value)
	slider.value_changed.connect(func(current: float): value.text = "%.2f" % current)
	sliders[key] = slider
	value_labels[key] = value
	add_child(row)

func populate_style(style: Dictionary) -> void:
	for field in STYLE_FIELDS:
		var key := str(field.key)
		var slider: HSlider = sliders.get(key)
		if slider == null:
			continue
		var raw: Variant = style.get(key, STYLE_DEFAULT)
		var value := clampf(float(raw) if raw is float or raw is int else STYLE_DEFAULT, 0.0, 1.0)
		slider.set_value_no_signal(value)
		var label: Label = value_labels.get(key)
		if label != null:
			label.text = "%.2f" % value

func style_values() -> Dictionary:
	var result := {}
	for field in STYLE_FIELDS:
		var key := str(field.key)
		var slider: HSlider = sliders.get(key)
		result[key] = STYLE_DEFAULT if slider == null else slider.value
	return result

func configure(service) -> void:
	_disconnect_service()
	_service = service
	if service != null:
		if service.has_signal("request_finished") and not service.request_finished.is_connected(_on_request_finished):
			service.request_finished.connect(_on_request_finished)
		if service.has_signal("capabilities_received") and not service.capabilities_received.is_connected(_on_capabilities):
			service.capabilities_received.connect(_on_capabilities)
		_service_state = str(service.state)
	_available = _feature_available()
	_refresh_controls()
	if _ready_for_phrases():
		refresh_phrases()

func set_service_state(state: String) -> void:
	_service_state = state
	_refresh_controls()

func refresh() -> void:
	refresh_phrases()

func refresh_phrases() -> void:
	if not _ready_for_phrases():
		return
	var id: String = _service.request("expression.phrases.list", {}, 10.0)
	if id.is_empty():
		phrases_status.text = "短语列表加载失败。"
		return
	_requests[id] = {"type": "expression.phrases.list"}
	phrases_status.text = "正在加载短语…"

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	if _service.has_signal("capabilities_received") and _service.capabilities_received.is_connected(_on_capabilities):
		_service.capabilities_received.disconnect(_on_capabilities)

func _feature_available() -> bool:
	if _service == null:
		return false
	var features: Variant = _service.get("features")
	if not features is Dictionary:
		return false
	var feature: Variant = features.get("expression", {})
	return feature is Dictionary and bool(feature.get("available", false))

func _ready_for_phrases() -> bool:
	return _service != null and _service_state == "ready" and _available

func _refresh_controls() -> void:
	if refresh_button == null:
		return
	var ready := _service != null and _service_state == "ready"
	for field in STYLE_FIELDS:
		var slider: HSlider = sliders.get(str(field.key))
		if slider != null:
			slider.editable = ready
	refresh_button.disabled = not _ready_for_phrases()
	for check in phrase_checks:
		check.disabled = not _ready_for_phrases()
	if _service == null or _service_state != "ready":
		phrases_status.text = "后台服务未连接。"
	elif not _available:
		phrases_status.text = UNAVAILABLE_TEXT

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_available = _feature_available()
	_refresh_controls()
	if _ready_for_phrases() and phrase_checks.is_empty():
		refresh_phrases()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var command := str(_requests[id].get("type", ""))
	var button: Button = _requests[id].get("button")
	_requests.erase(id)
	var message := str(payload.get("message", ""))
	var code := str(payload.get("code", ""))
	if type == "error" and code in ["not_available", "unknown_command"]:
		if command == "expression.phrases.list":
			_available = false
			phrases_status.text = UNAVAILABLE_TEXT
		else:
			_available = false
			phrases_status.text = UNAVAILABLE_TEXT
			if button != null:
				button.set_pressed_no_signal(not button.button_pressed)
		_refresh_controls()
		return
	if command == "expression.phrases.list":
		if type == "result":
			var phrases: Variant = payload.get("phrases", [])
			_render_phrases(phrases if phrases is Array else [])
		elif message.is_empty():
			phrases_status.text = "短语列表加载失败。"
			_refresh_controls()
		else:
			phrases_status.text = "短语列表加载失败：%s" % message
			_refresh_controls()
		return
	if command == "expression.phrases.set_enabled":
		if type == "result":
			phrases_status.text = "短语开关已保存。"
		elif message.is_empty():
			phrases_status.text = "短语开关保存失败。"
		else:
			phrases_status.text = "短语开关保存失败：%s" % message
		if button != null:
			button.disabled = false
		_refresh_controls()

func _render_phrases(phrases: Array) -> void:
	for child in phrases_box.get_children():
		phrases_box.remove_child(child)
		child.queue_free()
	phrase_checks.clear()
	if phrases.is_empty():
		phrases_status.text = "还没有短语。"
		_refresh_controls()
		return
	var count := 0
	for item in phrases:
		if not item is Dictionary:
			continue
		var phrase_id := str(item.get("id", ""))
		if phrase_id.is_empty():
			continue
		var text := str(item.get("text", ""))
		var category := str(item.get("category", ""))
		var check := CheckButton.new()
		check.text = text if category.is_empty() else "%s（%s）" % [text, category]
		check.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		check.set_pressed_no_signal(bool(item.get("enabled", true)))
		check.toggled.connect(func(pressed: bool): _set_phrase_enabled(phrase_id, pressed, check))
		phrases_box.add_child(check)
		phrase_checks.append(check)
		count += 1
	phrases_status.text = "共 %d 条短语" % count
	_refresh_controls()

func _set_phrase_enabled(phrase_id: String, enabled: bool, check: CheckButton) -> void:
	if not _ready_for_phrases():
		check.set_pressed_no_signal(not enabled)
		return
	check.disabled = true
	var id: String = _service.request("expression.phrases.set_enabled", {
		"phrase_id": phrase_id,
		"enabled": enabled,
	}, 10.0)
	if id.is_empty():
		check.disabled = false
		phrases_status.text = "短语开关保存失败：后台服务不可用。"
		return
	_requests[id] = {"type": "expression.phrases.set_enabled", "button": check}
	phrases_status.text = "正在保存短语开关…"

func _apply_preset(name: String) -> void:
	var values := style_values()
	if name == "customer":
		values.casualness = clampf(float(values.casualness) + 0.2, 0.0, 1.0)
		values.warmth = clampf(float(values.warmth) + 0.1, 0.0, 1.0)
		values.directness = clampf(float(values.directness) - 0.1, 0.0, 1.0)
		values.emoji_frequency = clampf(float(values.emoji_frequency) + 0.1, 0.0, 1.0)
		preset_status.text = "已调低客服感，记得保存设置。"
	elif name == "noisy":
		values.playfulness = clampf(float(values.playfulness) - 0.25, 0.0, 1.0)
		values.emoji_frequency = clampf(float(values.emoji_frequency) - 0.3, 0.0, 1.0)
		values.sentence_length = clampf(float(values.sentence_length) - 0.1, 0.0, 1.0)
		preset_status.text = "已调低热闹程度，记得保存设置。"
	else:
		for field in STYLE_FIELDS:
			values[str(field.key)] = STYLE_DEFAULT
		preset_status.text = "已恢复默认风格，记得保存设置。"
	_apply_values(values)

func _apply_values(values: Dictionary) -> void:
	for field in STYLE_FIELDS:
		var key := str(field.key)
		var slider: HSlider = sliders.get(key)
		if slider == null:
			continue
		slider.set_value_no_signal(clampf(float(values.get(key, STYLE_DEFAULT)), 0.0, 1.0))
		var label: Label = value_labels.get(key)
		if label != null:
			label.text = "%.2f" % slider.value
