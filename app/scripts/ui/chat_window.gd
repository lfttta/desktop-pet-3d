extends Window
## Chat window. Controls stay disabled until the service advertises history and
## a generator, but streaming frames render as they arrive.

signal retry_requested
signal state_updated(state_delta: Dictionary)
## Carries the animation a reply asked for up to the pet window.
signal action_requested(action: String)

const ChatSession = preload("res://scripts/chat/chat_session.gd")
const UNAVAILABLE_TEXT := "聊天功能尚未接入。"
const HISTORY_LIMIT := 50

var status: Label
var persona_label: Label
var messages: VBoxContainer
var scroll: ScrollContainer
var empty_state: Label
var input: LineEdit
var send_button: Button
var stop_button: Button
var retry_message_button: Button
var _service
var _session
var _settings: Dictionary = {}
var _persona_display := "Godette"
var _active_assistant: Label
var _last_user_text := ""
var _streaming := false
var _last_retryable := false
var _chat_blocked := false

func _ready() -> void:
	title = "Pet3D 聊天"
	size = Vector2i(520, 400)
	min_size = size
	content_scale_size = Vector2i(520, 400)
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
	scroll = ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.custom_minimum_size = Vector2(0, 170)
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
	stop_button = Button.new()
	stop_button.text = "停止"
	stop_button.disabled = true
	stop_button.pressed.connect(_stop)
	input_row.add_child(stop_button)
	var actions := HBoxContainer.new()
	column.add_child(actions)
	retry_message_button = Button.new()
	retry_message_button.text = "重试上一条"
	retry_message_button.visible = false
	retry_message_button.pressed.connect(_retry_message)
	actions.add_child(retry_message_button)
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
		_session.delta_received.connect(_on_delta)
		_session.accepted.connect(_on_accepted)
		_session.completed.connect(_on_completed)
		_session.error_received.connect(_on_error)
		_session.cancelled.connect(_on_cancelled)
		_session.unavailable.connect(_on_unavailable)
		_session.failed.connect(_on_failed)
		_session.history_loaded.connect(_on_history_loaded)
		_session.history_unavailable.connect(_on_history_unavailable)
		_session.bubble_received.connect(_on_bubble)
		_session.action_requested.connect(_on_action_requested)
	_session.configure(service)
	if service and service.has_signal("capabilities_received") and not service.capabilities_received.is_connected(_on_capabilities):
		service.capabilities_received.connect(_on_capabilities)
	_update_persona(service.personas if service else [])
	_refresh_availability()

func update_settings(settings: Dictionary) -> void:
	_settings = settings.duplicate(true)
	_update_persona(_service.personas if _service else [])
	_refresh_availability()

func show_status(detail: String) -> void:
	status.text = detail
	_refresh_availability()

func open() -> void:
	show()
	grab_focus()
	_refresh_availability()
	_load_history()

## Puts the caret in the message field, for a bubble click that asked to talk.
func focus_input() -> void:
	if input != null:
		input.grab_focus()

## Sends text that came from somewhere else (the desktop bubble) through the
## same path as the input box, so history, streaming and retry all apply.
## Returns false when chat is not available; the text stays in the field so the
## window can show it instead of silently dropping the line.
func submit_text(text: String) -> bool:
	if input == null:
		return false
	var body := text.strip_edges()
	if body.is_empty():
		return false
	input.text = body
	if not _chat_available():
		_refresh_availability()
		return false
	_send()
	return true

func _on_capabilities(features: Dictionary, personas: Array) -> void:
	var chat: Variant = features.get("chat", {})
	_chat_blocked = not (chat is Dictionary and bool(chat.get("available", false)))
	_update_persona(personas)
	_refresh_availability()

func _update_persona(personas: Array) -> void:
	var persona_id := str(_settings.get("persona_id", "godette"))
	_persona_display = persona_id if not persona_id.is_empty() else "未设置"
	for item in personas:
		if item is Dictionary and str(item.get("id", "")) == persona_id:
			_persona_display = str(item.get("display_name", persona_id))
			break
	var suffix := "" if _chat_available() else "（未接入）"
	persona_label.text = "人格：%s%s" % [_persona_display, suffix]

func _chat_available() -> bool:
	return _service != null and _service.state == "ready" and _session != null and _session.is_available() and not _chat_blocked

func _refresh_availability() -> void:
	var available := _chat_available()
	input.editable = available and not _streaming
	send_button.disabled = not available or _streaming
	stop_button.disabled = not _streaming
	input.placeholder_text = "输入消息…" if available else UNAVAILABLE_TEXT
	retry_message_button.visible = _last_retryable and not _last_user_text.is_empty()
	if empty_state and empty_state.visible:
		if available:
			empty_state.text = "尚无对话。"
		else:
			empty_state.text = "尚无对话。聊天功能尚未接入。"
	_update_persona(_service.personas if _service else [])

func _send() -> void:
	if _session == null or not _chat_available() or _streaming:
		return
	var text := input.text.strip_edges()
	if text.is_empty():
		return
	var id: String = _session.send(input.text)
	if id.is_empty():
		return
	_last_user_text = text
	_last_retryable = false
	_streaming = true
	_active_assistant = null
	status.text = "正在回复…"
	input.clear()
	_refresh_availability()

func _stop() -> void:
	if _session == null or not _streaming:
		return
	status.text = "正在停止…"
	_session.cancel()

func _retry_message() -> void:
	if _last_user_text.is_empty():
		return
	_retry_message_button_hide()
	input.text = _last_user_text
	_send()

func _retry_message_button_hide() -> void:
	_last_retryable = false
	retry_message_button.visible = false

func _load_history() -> void:
	if _session == null or _service == null or _service.state != "ready" or _streaming:
		return
	_session.load_history(HISTORY_LIMIT)

func _on_user_message(text: String) -> void:
	_add_message("你：", text)
	_scroll_to_bottom()

func _on_delta(text: String) -> void:
	if _active_assistant == null:
		_active_assistant = _add_message(_persona_display + "：", "")
	_active_assistant.text += text
	_scroll_to_bottom()

func _on_assistant_message(text: String) -> void:
	if _active_assistant != null:
		_active_assistant.text = _persona_display + "：" + text
		_active_assistant = null
	else:
		_add_message(_persona_display + "：", text)
	_scroll_to_bottom()
	_streaming = false
	_refresh_availability()

func _on_bubble(text: String) -> void:
	if text.strip_edges().is_empty():
		return
	_add_message(_persona_display + "：", text)
	_scroll_to_bottom()
	status.text = "主动消息"

func _on_accepted(_id: String, _request_id: String) -> void:
	status.text = "正在回复…"

## Forward the reply's animation request; the pet window owns the decision.
func _on_action_requested(action: String) -> void:
	if not action.is_empty():
		action_requested.emit(action)

func _on_completed(_message: String, usage: Dictionary, memory_ops: Array, state_delta: Dictionary) -> void:
	_streaming = false
	_active_assistant = null
	var total := int(usage.get("total_tokens", 0))
	var notes: Array[String] = []
	# With auto_capture off the service hands back candidates instead of writing
	# them, and the reply used to say "记忆 N 条" while nothing was stored. Offer
	# the choice instead of counting them.
	var pending := pending_memory_candidates(memory_ops)
	if not pending.is_empty():
		_render_memory_candidates(pending)
		notes.append("%d 条记忆待确认" % pending.size())
	elif not memory_ops.is_empty():
		notes.append("记忆 %d 条" % memory_ops.size())
	if bool(state_delta.get("applied", false)):
		notes.append("状态已更新")
	status.text = "回复完成" if total <= 0 else "回复完成（%d tokens）" % total
	if not notes.is_empty():
		status.text += "，" + "，".join(PackedStringArray(notes))
	_refresh_availability()
	if state_delta.has("presentation"):
		state_updated.emit(state_delta)

## The candidates a reply asked us to remember but did not store.
##
## Static so the filter can be tested without building the whole window.
static func pending_memory_candidates(memory_ops: Array) -> Array[String]:
	var lines: Array[String] = []
	for op in memory_ops:
		if typeof(op) != TYPE_DICTIONARY:
			continue
		var entry: Dictionary = op
		if str(entry.get("operation", "")) != "propose":
			continue
		if str(entry.get("status", "")) != "pending":
			continue
		var text := str(entry.get("content", "")).strip_edges()
		if not text.is_empty():
			lines.append(text)
	return lines

func _render_memory_candidates(lines: Array[String]) -> void:
	var block := VBoxContainer.new()
	var title := Label.new()
	title.text = "AI 想记住："
	block.add_child(title)
	for line in lines:
		var row := HBoxContainer.new()
		var label := Label.new()
		label.text = line
		label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		row.add_child(label)
		var keep := Button.new()
		keep.text = "记住"
		var drop := Button.new()
		drop.text = "忽略"
		keep.pressed.connect(_remember_candidate.bind(label, keep, drop, line))
		drop.pressed.connect(_dismiss_candidate.bind(label, keep, drop, line))
		row.add_child(keep)
		row.add_child(drop)
		block.add_child(row)
	messages.add_child(block)
	_scroll_to_bottom()

func _remember_candidate(label: Label, keep: Button, drop: Button, text: String) -> void:
	keep.disabled = true
	drop.disabled = true
	if _service == null:
		label.text = text + "（服务未连接）"
		return
	if _service.request("memory.propose", {"text": text}, 10.0).is_empty():
		label.text = text + "（保存失败）"
		return
	label.text = text + "（已记住）"

func _dismiss_candidate(label: Label, keep: Button, drop: Button, text: String) -> void:
	keep.disabled = true
	drop.disabled = true
	label.text = text + "（已忽略）"

func _on_error(_code: String, message: String, retryable: bool) -> void:
	_last_retryable = retryable
	status.text = message + "（可重试）" if retryable else message
	_streaming = false
	_active_assistant = null
	_refresh_availability()

func _on_cancelled() -> void:
	_streaming = false
	_active_assistant = null
	status.text = "已停止生成。"
	_refresh_availability()

func _on_unavailable(_message: String) -> void:
	_chat_blocked = true
	_streaming = false
	_active_assistant = null
	status.text = UNAVAILABLE_TEXT
	_refresh_availability()

func _on_failed(message: String) -> void:
	status.text = message
	_streaming = false
	_active_assistant = null
	_refresh_availability()

func _on_history_loaded(items: Array) -> void:
	if _streaming:
		return
	_clear_messages()
	if items.is_empty():
		empty_state.text = "尚无对话。"
		empty_state.visible = true
		return
	for item in items:
		if not item is Dictionary:
			continue
		var text := str(item.get("text", ""))
		if text.is_empty():
			continue
		var role := str(item.get("role", ""))
		var prefix := ""
		if role == "user":
			prefix = "你："
		elif role == "assistant":
			prefix = _persona_display + "："
		_add_message(prefix, text)
	_scroll_to_bottom()

func _on_history_unavailable(message: String) -> void:
	if message.is_empty():
		status.text = "历史记录未接入。"
	else:
		status.text = "历史记录未接入：%s" % message

func _clear_messages() -> void:
	for child in messages.get_children():
		if child == empty_state:
			continue
		messages.remove_child(child)
		child.queue_free()
	empty_state.visible = true

func _add_message(prefix: String, text: String) -> Label:
	if empty_state:
		empty_state.visible = false
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.text = prefix + text
	messages.add_child(label)
	return label

func _scroll_to_bottom() -> void:
	if scroll == null:
		return
	await get_tree().process_frame
	scroll.scroll_vertical = int(scroll.get_v_scroll_bar().max_value)
