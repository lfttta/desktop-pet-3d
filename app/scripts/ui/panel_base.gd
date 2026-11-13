extends VBoxContainer
## 面板公共骨架：服务注入、信号连接/断开、能力可用性判断与请求回执分发。
## 表达/成长/用量三个面板共用这里的样板，子类只保留自己的控件、命令与
## 渲染逻辑；memory_panel 仍自带一份同类样板，尚未迁移到这里。
## 未注入服务（configure(null) 或从未 configure）时面板保持
## “后台服务未连接”的禁用状态，不会报错。

## 服务回执里表示“功能未接入”的错误码。
const UNAVAILABLE_CODES := ["not_available", "unknown_command"]
const SECTION_FONT_SIZE := 17

var _service
var _requests := {}
var _service_state := "stopped"
## 主能力是否可用；收到 not_available/unknown_command 后置为 false。
var _available := false

func _ready() -> void:
	add_theme_constant_override("separation", 8)

## 主能力名（capabilities.features 里的键），子类覆盖。
func _feature_key() -> String:
	return ""

## 显式注入服务客户端。重复调用会先断开旧服务，避免信号被连接两次。
func configure(service) -> void:
	_disconnect_service()
	_service = service
	if service != null:
		if service.has_signal("request_finished") and not service.request_finished.is_connected(_on_request_finished):
			service.request_finished.connect(_on_request_finished)
		if service.has_signal("capabilities_received") and not service.capabilities_received.is_connected(_on_capabilities):
			service.capabilities_received.connect(_on_capabilities)
		_service_state = str(service.state)
	_refresh_capabilities()
	_refresh_controls()
	_after_configure()

func set_service_state(state: String) -> void:
	_service_state = state
	_refresh_controls()

## 子类覆盖：拉取自己的数据。
func refresh() -> void:
	pass

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	if _service.has_signal("capabilities_received") and _service.capabilities_received.is_connected(_on_capabilities):
		_service.capabilities_received.disconnect(_on_capabilities)

## name 为空时用主能力名；用量面板这类多能力面板显式传名字。
func _feature_available(name := "") -> bool:
	if _service == null:
		return false
	var features: Variant = _service.get("features")
	if not features is Dictionary:
		return false
	var key := name if not name.is_empty() else _feature_key()
	var feature: Variant = features.get(key, {})
	return feature is Dictionary and bool(feature.get("available", false))

func _service_ready() -> bool:
	return _service != null and _service_state == "ready"

func _ready_for_requests() -> bool:
	return _service_ready() and _available

## 重新读取能力表；多能力面板覆盖后补充自己的标志。
func _refresh_capabilities() -> void:
	_available = _feature_available()

## 子类覆盖：刷新控件禁用状态与占位文案。
func _refresh_controls() -> void:
	pass

## configure 之后的自动拉取；默认主能力可用就刷新。
func _after_configure() -> void:
	if _available:
		refresh()

## capabilities 到达之后的补充拉取；默认不做。
func _after_capabilities() -> void:
	pass

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_refresh_capabilities()
	_refresh_controls()
	_after_capabilities()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var entry: Dictionary = _requests[id]
	_requests.erase(id)
	var command := str(entry.get("type", ""))
	if type == "error" and str(payload.get("code", "")) in UNAVAILABLE_CODES:
		_mark_unavailable(command, entry)
		_refresh_controls()
		return
	_handle_reply(command, type, payload, entry)

## 子类覆盖：把命令标记为未接入并写占位文案（控件刷新由基类统一做）。
func _mark_unavailable(_command: String, _entry: Dictionary) -> void:
	_available = false

## 子类覆盖：按命令处理回执。entry 是发起请求时登记的上下文（按钮、id 等）。
func _handle_reply(_command: String, _type: String, _payload: Dictionary, _entry: Dictionary) -> void:
	pass

## 失败文案：没有原因时只用前缀，有原因时追加“：原因”。
func _failure_text(message: String, stem: String) -> String:
	return stem + "。" if message.is_empty() else "%s：%s" % [stem, message]

## 回执文案：成功用 ok_text，失败用 fail_stem。
func _reply_text(type: String, message: String, ok_text: String, fail_stem: String) -> String:
	return ok_text if type == "result" else _failure_text(message, fail_stem)

func _section(text: String) -> void:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", SECTION_FONT_SIZE)
	add_child(label)