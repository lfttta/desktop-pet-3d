extends Window

signal save_requested(patch: Dictionary)
signal recover_requested
signal retry_requested

const MemoryPanel = preload("res://scripts/ui/memory_panel.gd")
const ExpressionPanel = preload("res://scripts/ui/expression_panel.gd")
const GrowthPanel = preload("res://scripts/ui/growth_panel.gd")
const UsagePanel = preload("res://scripts/ui/usage_panel.gd")
const AI_DEFAULTS := {
	"base_url": "https://api.deepseek.com",
	"model": "deepseek-chat",
	"temperature": 0.8,
	"connect_timeout_ms": 5000,
	"request_timeout_ms": 30000,
	"daily_token_budget": 120000,
	"monthly_cost_budget": 15.0,
}
const MEMORY_DEFAULTS := {
	"enabled": true,
	"auto_capture": false,
}
const MEMORY_COMPACTION_DEFAULTS := {
	"enabled": false,
	"model_compress": false,
	"daily_limit": 20,
	"min_age_days": 7,
	"max_per_run": 10,
	"protect_importance": 0.8,
}
const MEMORY_ARCHIVE_DEFAULTS := {
	"enabled": true,
	"topic_stale_days": 14,
}
const EMBEDDING_DEFAULTS := {
	"enabled": false,
	"model": "",
	"dimensions": 0,
}
const GROWTH_DEFAULTS := {
	"enabled": false,
	"min_sessions": 3,
	"min_memories": 2,
	"max_step": 0.05,
	"cooldown_days": 7,
}
const AUTONOMY_DEFAULTS := {
	"enabled": false,
	"quiet_start": "23:00",
	"quiet_end": "08:00",
}

var tabs: TabContainer
var name_input: LineEdit
var display_choice: OptionButton
var size_slider: HSlider
var size_value: Label
var status: Label
var message: Label
var save_button: Button
var retry_button: Button
var ai_base_url: LineEdit
var ai_model: LineEdit
var ai_key: LineEdit
var ai_key_button: Button
var ai_clear_key_button: Button
var ai_test_button: Button
var ai_key_status: Label
var ai_temperature: HSlider
var ai_temperature_value: Label
var ai_connect_timeout: SpinBox
var ai_request_timeout: SpinBox
var ai_daily_tokens: SpinBox
var ai_monthly_budget: SpinBox
var memory_enabled: CheckButton
var memory_auto_capture: CheckButton
var compaction_enabled: CheckButton
var compaction_model_compress: CheckButton
var compaction_daily_limit: SpinBox
var compaction_min_age: SpinBox
var compaction_max_per_run: SpinBox
var compaction_protect: SpinBox
var archive_enabled: CheckButton
var archive_stale_days: SpinBox
var embedding_enabled: CheckButton
var embedding_model: LineEdit
var embedding_dimensions: SpinBox
var growth_enabled: CheckButton
var growth_min_sessions: SpinBox
var growth_min_memories: SpinBox
var growth_max_step: SpinBox
var growth_cooldown_days: SpinBox
var memory_panel
var expression_panel
var growth_panel
var usage_panel
var autonomy_enabled: CheckButton
var quiet_start_input: LineEdit
var quiet_end_input: LineEdit
var quiet_button: Button
var autonomy_status: Label
var _display_ids: Array[String] = []
var _service
var _requests := {}
var _service_state := "stopped"
var _ai_available := true
var _autonomy_available := false

func _ready() -> void:
	title = "Pet3D 设置"
	size = Vector2i(720, 560)
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
		margin.add_theme_constant_override("margin_" + side, 18)
	add_child(margin)
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 8)
	margin.add_child(column)
	var heading := Label.new()
	heading.text = "让桌宠适合你的桌面"
	heading.add_theme_font_size_override("font_size", 22)
	column.add_child(heading)
	tabs = TabContainer.new()
	tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	tabs.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	tabs.custom_minimum_size = Vector2(0, 300)
	column.add_child(tabs)
	_build_basic_page()
	_build_ai_page()
	_build_memory_page()
	_build_expression_page()
	_build_autonomy_page()
	_build_growth_page()
	_build_usage_page()
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(status)
	message = Label.new()
	message.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	message.custom_minimum_size.y = 30
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
	_bind_service()
	_refresh_controls()

func _page(title: String, node_name: String) -> VBoxContainer:
	var scroll := ScrollContainer.new()
	scroll.name = node_name
	scroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	tabs.add_child(scroll)
	tabs.set_tab_title(tabs.get_tab_count() - 1, title)
	var page := VBoxContainer.new()
	page.name = "Content"
	page.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page.add_theme_constant_override("separation", 10)
	scroll.add_child(page)
	return page

func _build_basic_page() -> void:
	var page := _page("基础", "BasicPage")
	name_input = LineEdit.new()
	name_input.max_length = 80
	_row(page, "称呼", name_input)
	display_choice = OptionButton.new()
	_row(page, "显示器", display_choice)
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
	_row(page, "显示大小", slider_row)

func _build_ai_page() -> void:
	var page := _page("AI", "AiPage")
	ai_base_url = LineEdit.new()
	ai_base_url.max_length = 2048
	_row(page, "服务地址", ai_base_url)
	ai_model = LineEdit.new()
	ai_model.max_length = 200
	_row(page, "模型", ai_model)
	var key_row := HBoxContainer.new()
	ai_key = LineEdit.new()
	ai_key.secret = true
	ai_key.placeholder_text = "输入 API Key（不写入设置文件）"
	ai_key.max_length = 4096
	ai_key.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	key_row.add_child(ai_key)
	ai_key_button = Button.new()
	ai_key_button.text = "保存密钥"
	ai_key_button.pressed.connect(_save_key)
	key_row.add_child(ai_key_button)
	ai_clear_key_button = Button.new()
	ai_clear_key_button.text = "清除"
	ai_clear_key_button.pressed.connect(_clear_key)
	key_row.add_child(ai_clear_key_button)
	row_with_label(page, "API Key", key_row)
	var temperature_row := HBoxContainer.new()
	ai_temperature = HSlider.new()
	ai_temperature.min_value = 0.0
	ai_temperature.max_value = 2.0
	ai_temperature.step = 0.05
	ai_temperature.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	ai_temperature_value = Label.new()
	ai_temperature_value.custom_minimum_size.x = 48
	temperature_row.add_child(ai_temperature)
	temperature_row.add_child(ai_temperature_value)
	ai_temperature.value_changed.connect(func(value: float): ai_temperature_value.text = "%.2f" % value)
	_row(page, "温度", temperature_row)
	ai_connect_timeout = _spin(1000, 600000, 500)
	_row(page, "连接超时（毫秒）", ai_connect_timeout)
	ai_request_timeout = _spin(1000, 600000, 1000)
	_row(page, "请求超时（毫秒）", ai_request_timeout)
	ai_daily_tokens = _spin(0, 100000000, 1000)
	_row(page, "每日 Token 预算", ai_daily_tokens)
	ai_monthly_budget = _spin(0, 1000000000, 0.5)
	_row(page, "每月费用预算", ai_monthly_budget)
	_section(page, "向量索引")
	embedding_enabled = CheckButton.new()
	embedding_enabled.text = "启用向量索引"
	_row(page, "向量索引", embedding_enabled)
	embedding_model = LineEdit.new()
	embedding_model.max_length = 200
	embedding_model.placeholder_text = "例如 text-embedding-3-small"
	_row(page, "嵌入模型", embedding_model)
	embedding_dimensions = _spin(0, 4096, 1)
	_row(page, "向量维度", embedding_dimensions)
	var ai_actions := HBoxContainer.new()
	ai_test_button = Button.new()
	ai_test_button.text = "测试连接"
	ai_test_button.pressed.connect(_test_key)
	ai_actions.add_child(ai_test_button)
	ai_key_status = Label.new()
	ai_key_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	ai_key_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	ai_actions.add_child(ai_key_status)
	page.add_child(ai_actions)

func _build_memory_page() -> void:
	var page := _page("记忆", "MemoryPage")
	memory_enabled = CheckButton.new()
	memory_enabled.text = "启用记忆"
	_row(page, "记忆", memory_enabled)
	memory_auto_capture = CheckButton.new()
	memory_auto_capture.text = "自动记录对话"
	_row(page, "自动记录", memory_auto_capture)
	_section(page, "整理与归档")
	compaction_enabled = CheckButton.new()
	compaction_enabled.text = "启用记忆整理"
	_row(page, "记忆整理", compaction_enabled)
	compaction_model_compress = CheckButton.new()
	compaction_model_compress.text = "使用模型压缩正文"
	_row(page, "模型压缩", compaction_model_compress)
	compaction_daily_limit = _spin(0, 10000, 1)
	_row(page, "每日上限", compaction_daily_limit)
	compaction_min_age = _spin(0, 3650, 1)
	_row(page, "最小保留天数", compaction_min_age)
	compaction_max_per_run = _spin(1, 100, 1)
	_row(page, "每次最多整理", compaction_max_per_run)
	compaction_protect = _spin(0.0, 1.0, 0.05)
	_row(page, "保护重要度", compaction_protect)
	archive_enabled = CheckButton.new()
	archive_enabled.text = "自动归档陈旧主题"
	_row(page, "自动归档", archive_enabled)
	archive_stale_days = _spin(1, 3650, 1)
	_row(page, "陈旧天数", archive_stale_days)
	memory_panel = MemoryPanel.new()
	memory_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page.add_child(memory_panel)

func _build_expression_page() -> void:
	var page := _page("表达", "ExpressionPage")
	expression_panel = ExpressionPanel.new()
	expression_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page.add_child(expression_panel)

func _build_autonomy_page() -> void:
	var page := _page("主动", "AutonomyPage")
	autonomy_enabled = CheckButton.new()
	autonomy_enabled.text = "启用主动行为"
	_row(page, "主动行为", autonomy_enabled)
	quiet_start_input = LineEdit.new()
	quiet_start_input.max_length = 5
	quiet_start_input.placeholder_text = "HH:MM"
	_row(page, "安静开始", quiet_start_input)
	quiet_end_input = LineEdit.new()
	quiet_end_input.max_length = 5
	quiet_end_input.placeholder_text = "HH:MM"
	_row(page, "安静结束", quiet_end_input)
	var quiet_row := HBoxContainer.new()
	quiet_button = Button.new()
	quiet_button.text = "安静 30 分钟"
	quiet_button.pressed.connect(_set_quiet)
	quiet_row.add_child(quiet_button)
	autonomy_status = Label.new()
	autonomy_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	autonomy_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	quiet_row.add_child(autonomy_status)
	page.add_child(quiet_row)

func _build_growth_page() -> void:
	var page := _page("成长", "GrowthPage")
	growth_enabled = CheckButton.new()
	growth_enabled.text = "启用人格成长"
	_row(page, "成长", growth_enabled)
	growth_min_sessions = _spin(1, 1000, 1)
	_row(page, "最少会话数", growth_min_sessions)
	growth_min_memories = _spin(1, 1000, 1)
	_row(page, "最少记忆数", growth_min_memories)
	growth_max_step = _spin(0.001, 0.2, 0.001)
	_row(page, "单次最大变化", growth_max_step)
	growth_cooldown_days = _spin(0, 365, 1)
	_row(page, "冷却天数", growth_cooldown_days)
	growth_panel = GrowthPanel.new()
	growth_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page.add_child(growth_panel)

func _build_usage_page() -> void:
	var page := _page("用量与诊断", "UsagePage")
	usage_panel = UsagePanel.new()
	usage_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	page.add_child(usage_panel)

func _row(column: VBoxContainer, text: String, control: Control) -> void:
	row_with_label(column, text, control)

func row_with_label(column: VBoxContainer, text: String, control: Control) -> void:
	var row := HBoxContainer.new()
	var label := Label.new()
	label.text = text
	label.custom_minimum_size.x = 120
	row.add_child(label)
	control.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(control)
	column.add_child(row)

func _section(column: VBoxContainer, text: String) -> void:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", 18)
	column.add_child(label)

func _spin(min_value: float, max_value: float, step: float) -> SpinBox:
	var box := SpinBox.new()
	box.min_value = min_value
	box.max_value = max_value
	box.step = step
	box.custom_minimum_size.x = 140
	return box

func _panels() -> Array:
	return [memory_panel, expression_panel, growth_panel, usage_panel]

func _bind_service() -> void:
	var host := get_parent()
	if host == null:
		return
	for child in host.get_children():
		if child.has_method("request") and child.has_signal("request_finished") and child.has_signal("capabilities_received"):
			_service = child
			break
	if _service == null:
		return
	_service_state = str(_service.state)
	if not _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.connect(_on_request_finished)
	if not _service.capabilities_received.is_connected(_on_capabilities):
		_service.capabilities_received.connect(_on_capabilities)
	_autonomy_available = _feature_available("autonomy")
	for panel in _panels():
		if panel and panel.has_method("configure"):
			panel.configure(_service)
			if panel.has_method("set_service_state"):
				panel.set_service_state(_service_state)

func _feature_available(name: String) -> bool:
	if _service == null:
		return false
	var features: Variant = _service.get("features")
	if not features is Dictionary:
		return false
	var feature: Variant = features.get(name, {})
	return feature is Dictionary and bool(feature.get("available", false))

func populate(settings: Dictionary, displays: Array) -> void:
	name_input.text = str(settings.get("user_name", "你"))
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
	var ai := _group(settings, "ai")
	ai_base_url.text = str(ai.get("base_url", AI_DEFAULTS.base_url))
	ai_model.text = str(ai.get("model", AI_DEFAULTS.model))
	ai_temperature.value = clampf(float(ai.get("temperature", AI_DEFAULTS.temperature)), 0.0, 2.0)
	ai_temperature_value.text = "%.2f" % ai_temperature.value
	ai_connect_timeout.value = float(ai.get("connect_timeout_ms", AI_DEFAULTS.connect_timeout_ms))
	ai_request_timeout.value = float(ai.get("request_timeout_ms", AI_DEFAULTS.request_timeout_ms))
	ai_daily_tokens.value = float(ai.get("daily_token_budget", AI_DEFAULTS.daily_token_budget))
	ai_monthly_budget.value = float(ai.get("monthly_cost_budget", AI_DEFAULTS.monthly_cost_budget))
	var embedding := _group(ai, "embedding")
	embedding_enabled.button_pressed = bool(embedding.get("enabled", EMBEDDING_DEFAULTS.enabled))
	embedding_model.text = str(embedding.get("model", EMBEDDING_DEFAULTS.model))
	embedding_dimensions.value = float(embedding.get("dimensions", EMBEDDING_DEFAULTS.dimensions))
	ai_key.clear()
	var memory := _group(settings, "memory")
	memory_enabled.button_pressed = bool(memory.get("enabled", MEMORY_DEFAULTS.enabled))
	memory_auto_capture.button_pressed = bool(memory.get("auto_capture", MEMORY_DEFAULTS.auto_capture))
	var compaction := _group(memory, "compaction")
	compaction_enabled.button_pressed = bool(compaction.get("enabled", MEMORY_COMPACTION_DEFAULTS.enabled))
	compaction_model_compress.button_pressed = bool(compaction.get("model_compress", MEMORY_COMPACTION_DEFAULTS.model_compress))
	compaction_daily_limit.value = float(compaction.get("daily_limit", MEMORY_COMPACTION_DEFAULTS.daily_limit))
	compaction_min_age.value = float(compaction.get("min_age_days", MEMORY_COMPACTION_DEFAULTS.min_age_days))
	compaction_max_per_run.value = float(compaction.get("max_per_run", MEMORY_COMPACTION_DEFAULTS.max_per_run))
	compaction_protect.value = float(compaction.get("protect_importance", MEMORY_COMPACTION_DEFAULTS.protect_importance))
	var archive := _group(memory, "archive")
	archive_enabled.button_pressed = bool(archive.get("enabled", MEMORY_ARCHIVE_DEFAULTS.enabled))
	archive_stale_days.value = float(archive.get("topic_stale_days", MEMORY_ARCHIVE_DEFAULTS.topic_stale_days))
	var expression := _group(settings, "expression")
	if expression_panel:
		expression_panel.populate_style(_group(expression, "style"))
	var growth := _group(settings, "growth")
	growth_enabled.button_pressed = bool(growth.get("enabled", GROWTH_DEFAULTS.enabled))
	growth_min_sessions.value = float(growth.get("min_sessions", GROWTH_DEFAULTS.min_sessions))
	growth_min_memories.value = float(growth.get("min_memories", GROWTH_DEFAULTS.min_memories))
	growth_max_step.value = float(growth.get("max_step", GROWTH_DEFAULTS.max_step))
	growth_cooldown_days.value = float(growth.get("cooldown_days", GROWTH_DEFAULTS.cooldown_days))
	if growth_panel:
		growth_panel.populate_locks(growth.get("locked_traits", []))
	var autonomy := _group(settings, "autonomy")
	autonomy_enabled.button_pressed = bool(autonomy.get("enabled", AUTONOMY_DEFAULTS.enabled))
	quiet_start_input.text = str(autonomy.get("quiet_start", AUTONOMY_DEFAULTS.quiet_start))
	quiet_end_input.text = str(autonomy.get("quiet_end", AUTONOMY_DEFAULTS.quiet_end))

func _group(settings: Dictionary, name: String) -> Dictionary:
	var value: Variant = settings.get(name, {})
	if value is Dictionary:
		return value
	return {}

func _save() -> void:
	var quiet_start := quiet_start_input.text.strip_edges()
	var quiet_end := quiet_end_input.text.strip_edges()
	if not _valid_time(quiet_start) or not _valid_time(quiet_end):
		show_message("安静时段需使用 HH:MM 格式。", true)
		return
	var patch := {
		"user_name": name_input.text,
		"scale": size_slider.value,
		"screen_id": _display_ids[display_choice.selected],
		"ai": {
			"base_url": ai_base_url.text.strip_edges(),
			"model": ai_model.text.strip_edges(),
			"temperature": ai_temperature.value,
			"connect_timeout_ms": int(ai_connect_timeout.value),
			"request_timeout_ms": int(ai_request_timeout.value),
			"daily_token_budget": int(ai_daily_tokens.value),
			"monthly_cost_budget": ai_monthly_budget.value,
			"embedding": {
				"enabled": embedding_enabled.button_pressed,
				"model": embedding_model.text.strip_edges(),
				"dimensions": int(embedding_dimensions.value),
			},
		},
		"memory": {
			"enabled": memory_enabled.button_pressed,
			"auto_capture": memory_auto_capture.button_pressed,
			"compaction": {
				"enabled": compaction_enabled.button_pressed,
				"model_compress": compaction_model_compress.button_pressed,
				"daily_limit": int(compaction_daily_limit.value),
				"min_age_days": int(compaction_min_age.value),
				"max_per_run": int(compaction_max_per_run.value),
				"protect_importance": compaction_protect.value,
			},
			"archive": {
				"enabled": archive_enabled.button_pressed,
				"topic_stale_days": int(archive_stale_days.value),
			},
		},
		"growth": {
			"enabled": growth_enabled.button_pressed,
			"min_sessions": int(growth_min_sessions.value),
			"min_memories": int(growth_min_memories.value),
			"max_step": growth_max_step.value,
			"cooldown_days": int(growth_cooldown_days.value),
		},
		"autonomy": {
			"enabled": autonomy_enabled.button_pressed,
			"quiet_start": quiet_start,
			"quiet_end": quiet_end,
		},
	}
	if expression_panel:
		patch["expression"] = {"style": expression_panel.style_values()}
	save_requested.emit(patch)

func _valid_time(value: String) -> bool:
	if value.length() != 5 or value[2] != ":":
		return false
	var hours := value.substr(0, 2)
	var minutes := value.substr(3, 2)
	if not hours.is_valid_int() or not minutes.is_valid_int():
		return false
	return int(hours) >= 0 and int(hours) <= 23 and int(minutes) >= 0 and int(minutes) <= 59

func _save_key() -> void:
	var key := ai_key.text.strip_edges()
	if key.is_empty():
		show_message("请输入 API Key。", true)
		return
	var id := _send("ai.set_key", {"key": key}, 10.0)
	if id.is_empty():
		return
	ai_key.clear()
	ai_key_status.text = "正在保存密钥…"

func _clear_key() -> void:
	_send("ai.clear_key", {}, 10.0)

func _test_key() -> void:
	_send("ai.test", {}, 20.0)

func _set_quiet() -> void:
	if quiet_button:
		quiet_button.disabled = true
	var id := _send("autonomy.set_quiet", {"minutes": 30}, 10.0)
	if id.is_empty():
		_refresh_controls()

func _send(type: String, payload: Dictionary, timeout: float) -> String:
	if _service == null or _service_state != "ready":
		show_message("后台服务未连接。", true)
		return ""
	var id: String = _service.request(type, payload, timeout)
	if id.is_empty():
		show_message("请求发送失败。", true)
		return ""
	_requests[id] = type
	return id

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_autonomy_available = _feature_available("autonomy")
	_refresh_controls()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var command := str(_requests[id])
	_requests.erase(id)
	var detail := str(payload.get("message", ""))
	var code := str(payload.get("code", ""))
	if code == "not_available" or code == "unknown_command":
		if command.begins_with("ai."):
			_ai_available = false
			ai_key_status.text = "AI 未接入。"
		elif command == "autonomy.set_quiet":
			_autonomy_available = false
			autonomy_status.text = "主动行为未接入。"
		show_message("该功能未接入。", true)
		_refresh_controls()
		return
	if type != "result":
		if detail.is_empty():
			detail = "操作失败。"
		show_message(detail, true)
		_refresh_controls()
		return
	if command == "ai.set_key":
		ai_key_status.text = "密钥已保存（加密保存在本地）。"
		show_message("API Key 已保存。")
	elif command == "ai.clear_key":
		var cleared := bool(payload.get("cleared", false))
		ai_key_status.text = "密钥已清除。" if cleared else "没有可清除的密钥，或密钥文件未能删除。"
		show_message(ai_key_status.text, not cleared)
	elif command == "ai.test":
		var passed := bool(payload.get("ok", true))
		ai_key_status.text = "连接测试通过。" if passed else "连接测试失败。"
		show_message(ai_key_status.text, not passed)
	elif command == "autonomy.set_quiet":
		var until := str(payload.get("manual_quiet_until", ""))
		autonomy_status.text = "已进入安静模式。" if until.is_empty() else "安静至 %s。" % until
		show_message("已进入安静模式。")
	_refresh_controls()

func _refresh_controls() -> void:
	if ai_base_url == null:
		return
	var ready := _service != null and _service_state == "ready"
	ai_base_url.editable = ready
	ai_model.editable = ready
	ai_temperature.editable = ready
	ai_connect_timeout.editable = ready
	ai_request_timeout.editable = ready
	ai_daily_tokens.editable = ready
	ai_monthly_budget.editable = ready
	memory_enabled.disabled = not ready
	memory_auto_capture.disabled = not ready
	compaction_enabled.disabled = not ready
	compaction_model_compress.disabled = not ready
	compaction_daily_limit.editable = ready
	compaction_min_age.editable = ready
	compaction_max_per_run.editable = ready
	compaction_protect.editable = ready
	archive_enabled.disabled = not ready
	archive_stale_days.editable = ready
	embedding_enabled.disabled = not ready
	embedding_model.editable = ready
	embedding_dimensions.editable = ready
	growth_enabled.disabled = not ready
	growth_min_sessions.editable = ready
	growth_min_memories.editable = ready
	growth_max_step.editable = ready
	growth_cooldown_days.editable = ready
	autonomy_enabled.disabled = not ready
	quiet_start_input.editable = ready
	quiet_end_input.editable = ready
	var ai_enabled := ready and _ai_available
	ai_key.editable = ai_enabled
	ai_key_button.disabled = not ai_enabled
	ai_clear_key_button.disabled = not ai_enabled
	ai_test_button.disabled = not ai_enabled
	quiet_button.disabled = not (ready and _autonomy_available)
	if not ready:
		ai_key_status.text = "后台服务未连接。"
		autonomy_status.text = "后台服务未连接。"
		return
	if not _ai_available:
		ai_key_status.text = "AI 未接入。"
	elif ai_key_status.text in ["后台服务未连接。", "AI 未接入。"]:
		ai_key_status.text = ""
	if not _autonomy_available:
		autonomy_status.text = "主动行为未接入。"
	elif autonomy_status.text in ["后台服务未连接。", "主动行为未接入。"]:
		autonomy_status.text = ""

func show_status(state: String, detail: String) -> void:
	_service_state = state
	status.text = detail
	save_button.disabled = state != "ready"
	retry_button.disabled = state in ["ready", "starting", "restarting"]
	if state == "ready":
		_ai_available = true
		_autonomy_available = _feature_available("autonomy")
	for panel in _panels():
		if panel and panel.has_method("set_service_state"):
			panel.set_service_state(state)
	_refresh_controls()

func show_message(text: String, failed := false) -> void:
	message.text = text
	message.modulate = Color(1, 0.55, 0.5) if failed else Color(0.65, 0.9, 0.7)

func open() -> void:
	show()
	grab_focus()
	for panel in _panels():
		if panel and panel.has_method("set_service_state"):
			panel.set_service_state(_service_state)
		if panel and panel.has_method("refresh"):
			panel.refresh()
