extends "res://scripts/ui/panel_base.gd"
## Personality growth queue: pending candidates, applied history and trait
## locks. Every action is a service command; nothing is applied locally.

## Emitted once when a candidate is applied. Growth is otherwise only visible
## inside this panel, so the pet celebrates to make the change felt.
signal candidate_applied

const UNAVAILABLE_TEXT := "成长功能未接入。"
const LOCK_UNAVAILABLE_TEXT := "人格锁定未接入。"

var status: Label
var refresh_button: Button
var candidates_box: VBoxContainer
var history_box: VBoxContainer
var locks_box: VBoxContainer
var _lock_available := true
var _locked_traits := {}
var _lock_checks := {}
var _action_buttons: Array = []

func _ready() -> void:
	super._ready()
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

func _feature_key() -> String:
	return "growth"

## 能力刚到达且列表还是空的时候补一次拉取。
func _after_capabilities() -> void:
	if _available and candidates_box.get_child_count() == 0 and history_box.get_child_count() == 0:
		refresh()

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

## 锁定命令未接入只下线锁定开关；其余成长命令未接入则整个面板下线。
func _mark_unavailable(command: String, entry: Dictionary) -> void:
	if command == "personality.lock":
		_lock_available = false
		status.text = LOCK_UNAVAILABLE_TEXT
		var check: CheckButton = _lock_checks.get(str(entry.get("trait", "")))
		if check != null:
			check.set_pressed_no_signal(false)
		return
	_available = false
	status.text = UNAVAILABLE_TEXT

func _handle_reply(command: String, type: String, payload: Dictionary, entry: Dictionary) -> void:
	var button: Button = entry.get("button")
	var trait_name := str(entry.get("trait", ""))
	var locked := bool(entry.get("locked", false))
	var message := str(payload.get("message", ""))
	if command == "growth.list":
		if type == "result":
			var candidates: Variant = payload.get("candidates", [])
			var history: Variant = payload.get("history", [])
			_render(candidates if candidates is Array else [], history if history is Array else [])
			return
		status.text = _failure_text(message, "成长列表加载失败")
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
		else:
			status.text = _failure_text(message, "人格锁定保存失败")
		_refresh_controls()
		return
	if type == "result":
		status.text = "操作已提交，正在刷新…"
		if command == "growth.approve":
			candidate_applied.emit()
		refresh()
		return
	status.text = _failure_text(message, "操作失败")
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
