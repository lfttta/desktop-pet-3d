extends VBoxContainer
## Token usage, budget status and diagnostic bundle export.

const UNAVAILABLE_TEXT := "未接入"
const DIAG_UNAVAILABLE_TEXT := "诊断导出未接入。"

var status_label: Label
var refresh_button: Button
var today_label: Label
var month_label: Label
var limits_label: Label
var ratio_label: Label
var budget_label: Label
var budget_bar: ProgressBar
var price_label: Label
var models_label: Label
var days_label: Label
var export_button: Button
var open_button: Button
var path_label: Label
var included_label: Label
var diag_status: Label
var _service
var _requests := {}
var _service_state := "stopped"
var _usage_available := false
var _diag_available := false
var _last_path := ""

func _ready() -> void:
	add_theme_constant_override("separation", 8)
	var header := HBoxContainer.new()
	add_child(header)
	var title := Label.new()
	title.text = "用量与预算"
	title.add_theme_font_size_override("font_size", 17)
	header.add_child(title)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(spacer)
	status_label = Label.new()
	status_label.text = "状态：未接入"
	header.add_child(status_label)
	refresh_button = Button.new()
	refresh_button.text = "刷新"
	refresh_button.pressed.connect(refresh)
	header.add_child(refresh_button)
	today_label = _value_label("今日：—")
	month_label = _value_label("本月：—")
	limits_label = _value_label("预算：—")
	ratio_label = _value_label("已用：—")
	budget_bar = ProgressBar.new()
	budget_bar.min_value = 0.0
	budget_bar.max_value = 1.0
	budget_bar.step = 0.001
	budget_bar.value = 0.0
	budget_bar.show_percentage = false
	budget_bar.custom_minimum_size.y = 14
	add_child(budget_bar)
	budget_label = _value_label("本月预算已用 0%")
	price_label = _value_label("价格：未配置")
	models_label = _value_label("按模型：暂无数据")
	days_label = _value_label("按天：暂无数据")
	_section("诊断包")
	var diag_row := HBoxContainer.new()
	add_child(diag_row)
	export_button = Button.new()
	export_button.text = "导出诊断包"
	export_button.pressed.connect(_export_diag)
	diag_row.add_child(export_button)
	open_button = Button.new()
	open_button.text = "打开文件夹"
	open_button.disabled = true
	open_button.pressed.connect(_open_folder)
	diag_row.add_child(open_button)
	diag_status = Label.new()
	diag_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	diag_status.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	diag_row.add_child(diag_status)
	path_label = _value_label("诊断包：—")
	included_label = _value_label("包含：—")
	status_label.text = "状态：后台服务未连接"
	_refresh_controls()

func _section(text: String) -> void:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", 17)
	add_child(label)

func _value_label(text: String) -> Label:
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(label)
	return label

func configure(service) -> void:
	_disconnect_service()
	_service = service
	if service != null:
		if service.has_signal("request_finished") and not service.request_finished.is_connected(_on_request_finished):
			service.request_finished.connect(_on_request_finished)
		if service.has_signal("capabilities_received") and not service.capabilities_received.is_connected(_on_capabilities):
			service.capabilities_received.connect(_on_capabilities)
		_service_state = str(service.state)
	_usage_available = _feature_available("usage")
	_diag_available = _feature_available("diag")
	_refresh_controls()
	if _usage_available and _service_state == "ready":
		refresh()

func set_service_state(state: String) -> void:
	_service_state = state
	_refresh_controls()

func refresh() -> void:
	if not _ready_for_usage():
		status_label.text = "状态：" + UNAVAILABLE_TEXT
		return
	var id: String = _service.request("usage.get", {}, 10.0)
	if id.is_empty():
		status_label.text = "状态：加载失败"
		return
	_requests[id] = {"type": "usage.get"}
	status_label.text = "状态：加载中"

func folder_path() -> String:
	return _last_path.get_base_dir() if not _last_path.is_empty() else ""

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	if _service.has_signal("capabilities_received") and _service.capabilities_received.is_connected(_on_capabilities):
		_service.capabilities_received.disconnect(_on_capabilities)

func _feature_available(name: String) -> bool:
	if _service == null:
		return false
	var features: Variant = _service.get("features")
	if not features is Dictionary:
		return false
	var feature: Variant = features.get(name, {})
	return feature is Dictionary and bool(feature.get("available", false))

func _ready_for_usage() -> bool:
	return _service != null and _service_state == "ready" and _usage_available

func _ready_for_diag() -> bool:
	return _service != null and _service_state == "ready" and _diag_available

func _refresh_controls() -> void:
	if export_button == null:
		return
	refresh_button.disabled = not _ready_for_usage()
	export_button.disabled = not _ready_for_diag()
	if not _diag_available:
		diag_status.text = DIAG_UNAVAILABLE_TEXT
	elif diag_status.text == DIAG_UNAVAILABLE_TEXT:
		diag_status.text = ""
	if _service == null or _service_state != "ready":
		status_label.text = "状态：后台服务未连接"
	elif not _usage_available:
		status_label.text = "状态：" + UNAVAILABLE_TEXT

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_usage_available = _feature_available("usage")
	_diag_available = _feature_available("diag")
	_refresh_controls()
	if _usage_available and _service_state == "ready" and today_label.text.ends_with("—"):
		refresh()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var command := str(_requests[id].get("type", ""))
	_requests.erase(id)
	var message := str(payload.get("message", ""))
	var code := str(payload.get("code", ""))
	if type == "error" and code in ["not_available", "unknown_command"]:
		if command == "usage.get":
			_usage_available = false
			status_label.text = "状态：" + UNAVAILABLE_TEXT
		else:
			_diag_available = false
			diag_status.text = DIAG_UNAVAILABLE_TEXT
		_refresh_controls()
		return
	if command == "usage.get":
		if type == "result":
			_render(payload)
		elif message.is_empty():
			status_label.text = "状态：加载失败"
			_refresh_controls()
		else:
			status_label.text = "状态：加载失败（%s）" % message
			_refresh_controls()
		return
	if command == "diag.export":
		if type == "result":
			_render_export(payload)
		elif message.is_empty():
			diag_status.text = "诊断包导出失败。"
			_refresh_controls()
		else:
			diag_status.text = "诊断包导出失败：%s" % message
			_refresh_controls()

func _render(data: Dictionary) -> void:
	var today := _dict(data, "today")
	var month := _dict(data, "month")
	var limits := _dict(data, "limits")
	var ratio := _dict(data, "ratio")
	var currency := str(limits.get("currency", "USD"))
	today_label.text = "今日：%d tokens · %.2f %s · %d 次调用" % [
		int(today.get("tokens", 0)),
		float(today.get("cost", 0.0)),
		currency,
		int(today.get("calls", 0)),
	]
	month_label.text = "本月：%d tokens · %.2f %s · %d 次调用" % [
		int(month.get("tokens", 0)),
		float(month.get("cost", 0.0)),
		currency,
		int(month.get("calls", 0)),
	]
	limits_label.text = "预算：每日 %d tokens · 每月 %.2f %s" % [
		int(limits.get("daily_tokens", 0)),
		float(limits.get("monthly_cost", 0.0)),
		currency,
	]
	var token_ratio := float(ratio.get("tokens", 0.0))
	var cost_ratio := float(ratio.get("cost", 0.0))
	ratio_label.text = "已用：Tokens %d%% · 费用 %d%%" % [
		roundi(token_ratio * 100.0),
		roundi(cost_ratio * 100.0),
	]
	budget_bar.value = clampf(cost_ratio, 0.0, 1.0)
	budget_label.text = "本月预算已用 %d%%" % roundi(cost_ratio * 100.0)
	price_label.text = "价格：已配置（按 token 计价）" if bool(data.get("price_configured", false)) else "价格：未配置（费用可能为 0）"
	_render_models(data.get("by_model", []))
	_render_days(data.get("by_day", []))
	_apply_status(str(data.get("status", "ok")))
	_refresh_controls()

func _render_models(value: Variant) -> void:
	if not value is Array or value.is_empty():
		models_label.text = "按模型：暂无数据"
		return
	var lines: Array[String] = []
	var index := 0
	for item in value:
		if index >= 5:
			break
		if not item is Dictionary:
			continue
		lines.append("%s：%d tokens · %.2f" % [
			str(item.get("model", "未知")),
			int(item.get("tokens", 0)),
			float(item.get("cost", 0.0)),
		])
		index += 1
	models_label.text = "按模型：\n" + "\n".join(PackedStringArray(lines))

func _render_days(value: Variant) -> void:
	if not value is Array or value.is_empty():
		days_label.text = "按天：暂无数据"
		return
	var lines: Array[String] = []
	var start := maxi(0, value.size() - 7)
	for index in range(start, value.size()):
		var item: Variant = value[index]
		if not item is Dictionary:
			continue
		lines.append("%s：%d tokens · %.2f" % [
			str(item.get("date", "")),
			int(item.get("tokens", 0)),
			float(item.get("cost", 0.0)),
		])
	days_label.text = "按天：\n" + "\n".join(PackedStringArray(lines))

func _apply_status(value: String) -> void:
	var text := "状态：正常"
	var color := Color(0.65, 0.9, 0.7)
	if value == "warning":
		text = "状态：接近上限"
		color = Color(1.0, 0.82, 0.45)
	elif value == "hard_limit":
		text = "状态：已达上限"
		color = Color(1.0, 0.5, 0.45)
	status_label.text = text
	status_label.add_theme_color_override("font_color", color)

func _dict(source: Dictionary, key: String) -> Dictionary:
	var value: Variant = source.get(key, {})
	return value if value is Dictionary else {}

func _export_diag() -> void:
	if not _ready_for_diag():
		diag_status.text = DIAG_UNAVAILABLE_TEXT
		return
	export_button.disabled = true
	diag_status.text = "正在导出诊断包…"
	var id: String = _service.request("diag.export", {}, 20.0)
	if id.is_empty():
		export_button.disabled = false
		diag_status.text = "诊断包导出失败：后台服务不可用。"
		return
	_requests[id] = {"type": "diag.export"}

func _render_export(payload: Dictionary) -> void:
	_last_path = str(payload.get("path", ""))
	var bytes := int(payload.get("bytes", 0))
	var included: Variant = payload.get("included", [])
	if _last_path.is_empty():
		path_label.text = "诊断包：导出已完成，但未返回路径"
	else:
		path_label.text = "诊断包：%s（%d 字节）" % [_last_path, bytes]
	path_label.tooltip_text = _last_path
	var parts: Array[String] = []
	if included is Array:
		for item in included:
			parts.append(str(item))
	included_label.text = "包含：%s" % ", ".join(PackedStringArray(parts)) if not parts.is_empty() else "包含：—"
	open_button.disabled = _last_path.is_empty()
	export_button.disabled = false
	diag_status.text = "诊断包已导出。"
	_refresh_controls()

func _open_folder() -> void:
	var folder := folder_path()
	if folder.is_empty():
		diag_status.text = "还没有可打开的诊断包。"
		return
	var error := OS.shell_open(folder)
	if error != OK:
		diag_status.text = "无法打开文件夹：%s" % folder
	else:
		diag_status.text = "已打开诊断包所在文件夹。"
