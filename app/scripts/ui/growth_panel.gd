extends VBoxContainer
## Personality growth queue: pending candidates, applied history and trait
## locks. Every action is a service command; nothing is applied locally.

const UNAVAILABLE_TEXT := "成长功能未接入。"
const LOCK_UNAVAILABLE_TEXT := "人格锁定未接入。"

var status: Label
var refresh_button: Button
var candidates_box: VBoxContainer
var history_box: VBoxContainer
var locks_box: VBoxContainer
var _service
var _requests := {}
var _service_state := "stopped"
var _available := false
var _lock_available := true
var _locked_traits := {}
var _lock_checks := {}
var _action_buttons: Array = []

func _ready() -> void:
	add_theme_constant_override("separation", 8)
	var header := HBoxContainer.new()
	add_child(header)
	var title := Label.new()
	title.text = "人格成长"
	header.add_child(title)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(spacer)
	refresh_button = Button.new()
	refresh_button.text = "刷新"
	refresh_button.pressed.connect(refresh)
	header.add_child(refresh_button)
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(status)
	_section("待审批候选")
	candidates_box = VBoxContainer.new()
	candidates_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(candidates_box)
	_section("已应用历史")
	var history_scroll := ScrollContainer.new()
	history_scroll.custom_minimum_size = Vector2(0, 120)
	history_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(history_scroll)
	history_box = VBoxContainer.new()
	history_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	history_scroll.add_child(history_box)
	_section("人格锁定")
	locks_box = VBoxContainer.new()
	locks_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(locks_box)
	status.text = "后台服务未连接。"
	_refresh_controls()

func _section(text: String) -> void:
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override("font_size", 17)
	add_child(label)

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
	if _available:
		refresh()

func set_service_state(state: String) -> void:
	_service_state = state
	_refresh_controls()

func populate_locks(traits: Variant) -> void:
	if not traits is Array:
		return
	for item in traits:
		var trait_name := str(item)
		if trait_name.is_empty():
			continue
		_ensure_lock(trait_name)
		if _locked_traits.has(trait_name):
			# Keep the value learned from personality.lock during this session.
			var known: CheckButton = _lock_checks.get(trait_name)
			if known != null:
				known.set_pressed_no_signal(bool(_locked_traits[trait_name]))
			continue
		_locked_traits[trait_name] = true
		var check: CheckButton = _lock_checks.get(trait_name)
		if check != null:
			check.set_pressed_no_signal(true)

func refresh() -> void:
	if not _ready_for_requests():
		return
	var id: String = _service.request("growth.list", {}, 10.0)
	if id.is_empty():
		status.text = "成长列表加载失败。"
		return
	_requests[id] = {"type": "growth.list"}
	status.text = "正在加载成长数据…"

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
	var feature: Variant = features.get("growth", {})
	return feature is Dictionary and bool(feature.get("available", false))

func _ready_for_requests() -> bool:
	return _service != null and _service_state == "ready" and _available

func _refresh_controls() -> void:
	if refresh_button == null:
		return
	var enabled := _ready_for_requests()
	refresh_button.disabled = not enabled
	for entry in _action_buttons:
		var button: Button = entry.get("button")
		button.disabled = not enabled
	var lock_enabled := enabled and _lock_available
	for check in _lock_checks.values():
		var lock: CheckButton = check
		lock.disabled = not lock_enabled
	if _service == null or _service_state != "ready":
		status.text = "后台服务未连接。"
	elif not _available:
		status.text = UNAVAILABLE_TEXT

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_available = _feature_available()
	_refresh_controls()
	if _available and candidates_box.get_child_count() == 0 and history_box.get_child_count() == 0:
		refresh()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var command := str(_requests[id].get("type", ""))
	var button: Button = _requests[id].get("button")
	var trait_name := str(_requests[id].get("trait", ""))
	var locked := bool(_requests[id].get("locked", false))
	_requests.erase(id)
	var message := str(payload.get("message", ""))
	var code := str(payload.get("code", ""))
	if type == "error" and code in ["not_available", "unknown_command"]:
		if command == "personality.lock":
			_lock_available = false
			status.text = LOCK_UNAVAILABLE_TEXT
			var check: CheckButton = _lock_checks.get(trait_name)
			if check != null:
				check.set_pressed_no_signal(false)
		else:
			_available = false
			status.text = UNAVAILABLE_TEXT
		_refresh_controls()
		return
	if command == "growth.list":
		if type == "result":
			var candidates: Variant = payload.get("candidates", [])
			var history: Variant = payload.get("history", [])
			_render(candidates if candidates is Array else [], history if history is Array else [])
		elif message.is_empty():
			status.text = "成长列表加载失败。"
			_refresh_controls()
		else:
			status.text = "成长列表加载失败：%s" % message
			_refresh_controls()
		return
	if command == "personality.lock":
		if button != null:
			button.disabled = false
		if type == "result":
			_locked_traits[trait_name] = bool(payload.get("locked", locked))
			var check: CheckButton = _lock_checks.get(trait_name)
			if check != null:
				check.set_pressed_no_signal(bool(_locked_traits[trait_name]))
			status.text = "人格锁定已更新。"
		elif message.is_empty():
			status.text = "人格锁定保存失败。"
		else:
			status.text = "人格锁定保存失败：%s" % message
		_refresh_controls()
		return
	if type == "result":
		status.text = "操作已提交，正在刷新…"
		refresh()
	elif message.is_empty():
		status.text = "操作失败。"
		_refresh_controls()
	else:
		status.text = "操作失败：%s" % message
		_refresh_controls()

func _render(candidates: Array, history: Array) -> void:
	_clear(candidates_box)
	_clear(history_box)
	_action_buttons.clear()
	var count := 0
	for item in candidates:
		if not item is Dictionary:
			continue
		_add_candidate(item)
		count += 1
	if candidates.is_empty():
		var empty := Label.new()
		empty.text = "没有待审批的候选。"
		candidates_box.add_child(empty)
	var history_count := 0
	for item in history:
		if not item is Dictionary:
			continue
		_add_history(item)
		history_count += 1
	if history.is_empty():
		var empty := Label.new()
		empty.text = "还没有已应用的成长。"
		history_box.add_child(empty)
	status.text = "待审批 %d 项 · 历史 %d 条" % [count, history_count]
	_refresh_controls()

func _clear(box: VBoxContainer) -> void:
	for child in box.get_children():
		box.remove_child(child)
		child.queue_free()

func _add_candidate(item: Dictionary) -> void:
	var candidate_id := str(item.get("id", ""))
	if candidate_id.is_empty():
		return
	var trait_name := str(item.get("trait", ""))
	var delta := float(item.get("delta", 0.0))
	var row := HBoxContainer.new()
	var info := VBoxContainer.new()
	info.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.text = "%s %+.2f" % [trait_name, delta]
	info.add_child(label)
	var evidence := _evidence_text(item.get("evidence_ids", []))
	if not evidence.is_empty():
		var evidence_label := Label.new()
		evidence_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		evidence_label.text = evidence
		info.add_child(evidence_label)
	row.add_child(info)
	var approve := Button.new()
	approve.text = "采纳"
	approve.pressed.connect(func(): _approve(candidate_id, approve))
	row.add_child(approve)
	var reject := Button.new()
	reject.text = "忽略"
	reject.pressed.connect(func(): _reject(candidate_id, reject))
	row.add_child(reject)
	candidates_box.add_child(row)
	_action_buttons.append({"button": approve})
	_action_buttons.append({"button": reject})
	_ensure_lock(trait_name)

func _add_history(item: Dictionary) -> void:
	var history_id := str(item.get("id", ""))
	if history_id.is_empty():
		return
	var trait_name := str(item.get("trait", ""))
	var old_value := float(item.get("old_value", 0.0))
	var new_value := float(item.get("new_value", 0.0))
	var created := str(item.get("created_at", ""))
	var reverted := str(item.get("reverted_at", ""))
	var row := HBoxContainer.new()
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var text := "%s：%.2f → %.2f" % [trait_name, old_value, new_value]
	if not created.is_empty():
		text += " · %s" % created
	if not reverted.is_empty():
		text += " · 已回滚"
	label.text = text
	row.add_child(label)
	var rollback := Button.new()
	rollback.text = "回滚"
	rollback.disabled = not reverted.is_empty()
	rollback.pressed.connect(func(): _rollback(history_id, rollback))
	row.add_child(rollback)
	history_box.add_child(row)
	if reverted.is_empty():
		_action_buttons.append({"button": rollback})
	_ensure_lock(trait_name)

func _evidence_text(evidence: Variant) -> String:
	if not evidence is Array:
		return ""
	var ids: Array[String] = []
	for item in evidence:
		var text := str(item)
		if not text.is_empty():
			ids.append(text)
	if ids.is_empty():
		return ""
	return "证据：%s" % ", ".join(PackedStringArray(ids))

func _ensure_lock(trait_name: String) -> void:
	if trait_name.is_empty() or _lock_checks.has(trait_name):
		return
	var check := CheckButton.new()
	check.text = "锁定 %s" % trait_name
	check.set_pressed_no_signal(bool(_locked_traits.get(trait_name, false)))
	check.toggled.connect(func(pressed: bool): _set_lock(trait_name, pressed, check))
	locks_box.add_child(check)
	_lock_checks[trait_name] = check

func _approve(candidate_id: String, button: Button) -> void:
	_send_action("growth.approve", {"candidate_id": candidate_id}, button)
	status.text = "正在采纳…"

func _reject(candidate_id: String, button: Button) -> void:
	_send_action("growth.reject", {"candidate_id": candidate_id}, button)
	status.text = "正在忽略…"

func _rollback(history_id: String, button: Button) -> void:
	_send_action("growth.rollback", {"history_id": history_id}, button)
	status.text = "正在回滚…"

func _send_action(command: String, payload: Dictionary, button: Button) -> void:
	if not _ready_for_requests():
		status.text = UNAVAILABLE_TEXT
		return
	button.disabled = true
	var id: String = _service.request(command, payload, 10.0)
	if id.is_empty():
		button.disabled = false
		status.text = "操作失败：后台服务不可用。"
		return
	_requests[id] = {"type": command}

func _set_lock(trait_name: String, pressed: bool, check: CheckButton) -> void:
	if not _ready_for_requests():
		check.set_pressed_no_signal(not pressed)
		return
	if not _lock_available:
		check.set_pressed_no_signal(false)
		status.text = LOCK_UNAVAILABLE_TEXT
		return
	check.disabled = true
	var id: String = _service.request("personality.lock", {
		"trait": trait_name,
		"locked": pressed,
	}, 10.0)
	if id.is_empty():
		check.disabled = false
		check.set_pressed_no_signal(not pressed)
		status.text = "人格锁定保存失败：后台服务不可用。"
		return
	_requests[id] = {"type": "personality.lock", "trait": trait_name, "locked": pressed, "button": check}
	status.text = "正在保存锁定状态…"
