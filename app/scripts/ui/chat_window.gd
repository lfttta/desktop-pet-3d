extends Window
## Chat window skeleton. Controls stay visible but disabled until the service
## advertises the chat capability and a generator is implemented.

signal retry_requested

const ChatSession = preload("res://scripts/chat/chat_session.gd")
const UNAVAILABLE_TEXT := "聊天功能尚未接入（框架占位）。"

var status: Label
var persona_label: Label
var messages: VBoxContainer
var empty_state: Label
var input: LineEdit
var send_button: Button
var _service
var _session
var _settings: Dictionary = {}
var _persona_display := "Godette"

func _ready() -> void:
	title = "Pet3D 聊天"
	size = Vector2i(520, 400)
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
	column.add_theme_constant_override("separation", 12)
	margin.add_child(column)
	var header := HBoxContainer.new()
	column.add_child(header)
	var title_label := Label.new()
	title_label.text = "Pet3D 聊天"
	title_label.add_theme_font_size_override("font_size", 23)
	header.add_child(title_label)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(spacer)
	persona_label = Label.new()
	persona_label.text = "人格：Godette（未接入）"
	header.add_child(persona_label)
	var scroll := ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.custom_minimum_size = Vector2(0, 150)
	column.add_child(scroll)
	messages = VBoxContainer.new()
	messages.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(messages)
	empty_state = Label.new()
	empty_state.text = "尚无对话。聊天功能尚未接入。"
	empty_state.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	messages.add_child(empty_state)
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	column.add_child(status)
	var input_row := HBoxContainer.new()
	column.add_child(input_row)
	input = LineEdit.new()
	input.placeholder_text = UNAVAILABLE_TEXT
	input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	input.editable = false
	input.text_submitted.connect(func(_text: String): _send())
	input_row.add_child(input)
	send_button = Button.new()
	send_button.text = "发送"
	send_button.disabled = true
	send_button.pressed.connect(_send)
	input_row.add_child(send_button)
	var actions := HBoxContainer.new()
	column.add_child(actions)
	var retry := Button.new()
	retry.text = "重新连接后台服务"
	retry.pressed.connect(func(): retry_requested.emit())
	actions.add_child(retry)
	var close := Button.new()
	close.text = "关闭"
	close.pressed.connect(hide)
	actions.add_child(close)

func configure_chat(service, settings: Dictionary) -> void:
	_service = service
	_settings = settings.duplicate(true)
	if _session == null:
		_session = ChatSession.new()
		_session.user_message.connect(_on_user_message)
		_session.assistant_message.connect(_on_assistant_message)
		_session.unavailable.connect(_on_unavailable)
		_session.failed.connect(_on_failed)
	_session.configure(service)
	if service and not service.capabilities_received.is_connected(_on_capabilities):
		service.capabilities_received.connect(_on_capabilities)
	_update_persona(service.personas if service else [])
	_refresh_availability()

func update_settings(settings: Dictionary) -> void:
	_settings = settings.duplicate(true)
	_update_persona(_service.personas if _service else [])

func show_status(detail: String) -> void:
	status.text = detail
	_refresh_availability()

func open() -> void:
	show()
	grab_focus()
	_refresh_availability()

func _on_capabilities(_features: Dictionary, personas: Array) -> void:
	_update_persona(personas)
	_refresh_availability()

func _update_persona(personas: Array) -> void:
	var persona_id := str(_settings.get("persona_id", "godette"))
	_persona_display = persona_id if not persona_id.is_empty() else "未设置"
	for item in personas:
		if item is Dictionary and str(item.get("id", "")) == persona_id:
			_persona_display = str(item.get("display_name", persona_id))
			break
	persona_label.text = "人格：%s（未接入）" % _persona_display

func _refresh_availability() -> void:
	var ready: bool = _service != null and _service.state == "ready"
	var available: bool = ready and _session != null and _session.is_available()
	input.editable = available
	send_button.disabled = not available
	input.placeholder_text = "输入消息…" if available else UNAVAILABLE_TEXT
	if not available and empty_state:
		empty_state.text = "尚无对话。聊天功能尚未接入。"

func _send() -> void:
	if _session == null or not _session.is_available():
		return
	_session.send(input.text)
	input.clear()

func _on_user_message(text: String) -> void:
	if _session and _session.is_available():
		_add_message("你：", text)

func _on_assistant_message(text: String) -> void:
	_add_message(_persona_display + "：", text)

func _on_unavailable(_message: String) -> void:
	status.text = UNAVAILABLE_TEXT
	_refresh_availability()

func _on_failed(message: String) -> void:
	status.text = message

func _add_message(prefix: String, text: String) -> void:
	if empty_state:
		empty_state.visible = false
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.text = prefix + text
	messages.add_child(label)
