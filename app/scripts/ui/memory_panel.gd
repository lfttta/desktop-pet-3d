extends VBoxContainer
## Memory list with body text, category/importance metadata, correction and
## version history. Commands stay disabled when the service reports them as
## unavailable, and archived rows offer a restore action.

const LIST_LIMIT := 50
const UNAVAILABLE_TEXT := "记忆功能未接入。"
const HISTORY_UNAVAILABLE_TEXT := "历史版本未接入。"
const RESTORE_UNAVAILABLE_TEXT := "归档恢复未接入。"
const CORRECT_UNAVAILABLE_TEXT := "纠正功能未接入。"
const PROPOSE_UNAVAILABLE_TEXT := "记忆保存未接入。"
const SEARCH_UNAVAILABLE_TEXT := "记忆搜索未接入。"

var status: Label
var refresh_button: Button
var compact_button: Button
var jobs_button: Button
var rebuild_button: Button
var index_status_button: Button
var list_scroll: ScrollContainer
var list_box: VBoxContainer
var remember_input: LineEdit
var remember_button: Button
var search_input: LineEdit
var search_button: Button
var search_scroll: ScrollContainer
var search_box: VBoxContainer
var conflicts_box: VBoxContainer
var history_dialog: AcceptDialog
var history_list: VBoxContainer
var detail_dialog: AcceptDialog
var detail_label: Label
var correction_dialog: AcceptDialog
var correction_text: TextEdit
var _service
var _requests := {}
var _service_state := "stopped"
var _available := false
var _history_available := true
var _restore_available := true
var _correct_available := true
var _detail_available := true
var _archive_available := true
var _compact_available := true
var _jobs_available := true
var _index_available := true
var _resolve_available := true
var _propose_available := true
var _search_available := true
var _searching := false
var _correction_memory_id := ""
var _rows: Array = []
var _action_buttons: Array = []
var _conflict_ids := {}
var _conflicts_checked := false

func _ready() -> void:
	add_theme_constant_override("separation", 8)
	var header := HBoxContainer.new()
	add_child(header)
	var title := Label.new()
	title.text = "记忆列表"
	header.add_child(title)
	var spacer := Control.new()
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	header.add_child(spacer)
	refresh_button = Button.new()
	refresh_button.text = "刷新"
	refresh_button.disabled = true
	refresh_button.pressed.connect(refresh)
	header.add_child(refresh_button)
	compact_button = Button.new()
	compact_button.text = "整理"
	compact_button.disabled = true
	compact_button.pressed.connect(_request_compact)
	header.add_child(compact_button)
	jobs_button = Button.new()
	jobs_button.text = "任务"
	jobs_button.disabled = true
	jobs_button.pressed.connect(_request_jobs)
	header.add_child(jobs_button)
	rebuild_button = Button.new()
	rebuild_button.text = "重建索引"
	rebuild_button.disabled = true
	rebuild_button.pressed.connect(_request_index_rebuild)
	header.add_child(rebuild_button)
	index_status_button = Button.new()
	index_status_button.text = "索引状态"
	index_status_button.disabled = true
	index_status_button.pressed.connect(_request_index_status)
	header.add_child(index_status_button)
	var remember_row := HBoxContainer.new()
	add_child(remember_row)
	remember_input = LineEdit.new()
	remember_input.placeholder_text = "写一条想记住的事…"
	remember_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	remember_input.text_submitted.connect(func(_text: String): _request_propose())
	remember_row.add_child(remember_input)
	remember_button = Button.new()
	remember_button.text = "记住"
	remember_button.disabled = true
	remember_button.pressed.connect(_request_propose)
	remember_row.add_child(remember_button)
	var search_row := HBoxContainer.new()
	add_child(search_row)
	search_input = LineEdit.new()
	search_input.placeholder_text = "搜索记忆…"
	search_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	search_input.text_changed.connect(_on_search_changed)
	search_input.text_submitted.connect(func(_text: String): _request_search())
	search_row.add_child(search_input)
	search_button = Button.new()
	search_button.text = "搜索"
	search_button.disabled = true
	search_button.pressed.connect(_request_search)
	search_row.add_child(search_button)
	status = Label.new()
	status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	add_child(status)
	list_scroll = ScrollContainer.new()
	list_scroll.custom_minimum_size = Vector2(0, 170)
	list_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(list_scroll)
	list_box = VBoxContainer.new()
	list_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	list_scroll.add_child(list_box)
	search_scroll = ScrollContainer.new()
	search_scroll.custom_minimum_size = Vector2(0, 170)
	search_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	search_scroll.visible = false
	add_child(search_scroll)
	search_box = VBoxContainer.new()
	search_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	search_scroll.add_child(search_box)
	var conflicts_title := Label.new()
	conflicts_title.text = "待处理冲突"
	add_child(conflicts_title)
	conflicts_box = VBoxContainer.new()
	conflicts_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(conflicts_box)
	_build_correction_dialog()
	_build_history_dialog()
	_build_detail_dialog()
	status.text = "后台服务未连接。"
	_refresh_controls()

func _build_correction_dialog() -> void:
	correction_dialog = AcceptDialog.new()
	correction_dialog.title = "纠正记忆"
	correction_dialog.ok_button_text = "保存"
	correction_dialog.size = Vector2i(480, 240)
	var box := VBoxContainer.new()
	box.position = Vector2i(12, 12)
	box.size = Vector2i(456, 170)
	box.add_theme_constant_override("separation", 8)
	var hint := Label.new()
	hint.text = "改写后的内容会作为新版本保存，旧版本仍可在历史中查看。"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	box.add_child(hint)
	correction_text = TextEdit.new()
	correction_text.custom_minimum_size = Vector2(440, 120)
	correction_text.size_flags_vertical = Control.SIZE_EXPAND_FILL
	box.add_child(correction_text)
	correction_dialog.add_child(box)
	correction_dialog.confirmed.connect(_submit_correction)
	add_child(correction_dialog)

func _build_history_dialog() -> void:
	history_dialog = AcceptDialog.new()
	history_dialog.title = "记忆历史"
	history_dialog.ok_button_text = "关闭"
	history_dialog.size = Vector2i(540, 380)
	var scroll := ScrollContainer.new()
	scroll.position = Vector2i(12, 12)
	scroll.size = Vector2i(516, 300)
	history_list = VBoxContainer.new()
	history_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	history_list.add_theme_constant_override("separation", 8)
	scroll.add_child(history_list)
	history_dialog.add_child(scroll)
	add_child(history_dialog)

func _build_detail_dialog() -> void:
	detail_dialog = AcceptDialog.new()
	detail_dialog.title = "记忆详情"
	detail_dialog.ok_button_text = "关闭"
	detail_dialog.size = Vector2i(540, 360)
	var scroll := ScrollContainer.new()
	scroll.position = Vector2i(12, 12)
	scroll.size = Vector2i(516, 300)
	detail_label = Label.new()
	detail_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	detail_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(detail_label)
	detail_dialog.add_child(scroll)
	add_child(detail_dialog)

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

func refresh() -> void:
	if not _ready_for_requests():
		return
	var id: String = _service.request("memory.list", {"limit": LIST_LIMIT}, 10.0)
	if id.is_empty():
		status.text = "记忆列表加载失败。"
		return
	_requests[id] = {"type": "memory.list"}
	status.text = "正在加载…"

func _request_propose() -> void:
	if not _ready_for_requests() or not _propose_available:
		status.text = PROPOSE_UNAVAILABLE_TEXT
		return
	var text := remember_input.text.strip_edges()
	if text.is_empty():
		status.text = "记忆内容不能为空。"
		return
	var id: String = _service.request("memory.propose", {"text": text}, 10.0)
	if id.is_empty():
		status.text = "记录失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.propose"}
	status.text = "正在保存记忆…"

func _on_search_changed(new_text: String) -> void:
	if new_text.strip_edges().is_empty() and _searching:
		_clear_search()

func _request_search() -> void:
	var query := search_input.text.strip_edges()
	if query.is_empty():
		_clear_search()
		return
	if not _ready_for_requests() or not _search_available:
		status.text = SEARCH_UNAVAILABLE_TEXT
		return
	var id: String = _service.request("memory.search", {"query": query}, 10.0)
	if id.is_empty():
		status.text = "搜索失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.search"}
	_set_searching(true)
	status.text = "正在搜索…"

func _set_searching(active: bool) -> void:
	_searching = active
	list_scroll.visible = not active
	search_scroll.visible = active

func _clear_search_rows() -> void:
	for child in search_box.get_children():
		search_box.remove_child(child)
		child.queue_free()

func _clear_search() -> void:
	var was_searching := _searching
	_set_searching(false)
	_clear_search_rows()
	if not search_input.text.is_empty():
		search_input.text = ""
	if was_searching and _ready_for_requests():
		refresh()

func _render_search_results(results: Array) -> void:
	if not _searching:
		return
	_clear_search_rows()
	var count := 0
	for item in results:
		if not item is Dictionary:
			continue
		var text := str(item.get("text", item.get("snippet", "")))
		if text.is_empty():
			continue
		_add_search_row(item, text)
		count += 1
	if count == 0:
		status.text = "没有找到相关记忆。"
	else:
		status.text = "搜索到 %d 条记忆" % count
	_refresh_controls()

func _add_search_row(item: Dictionary, text: String) -> void:
	var row := HBoxContainer.new()
	var info := VBoxContainer.new()
	info.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	info.add_theme_constant_override("separation", 4)
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	label.text = text
	info.add_child(label)
	var meta := Label.new()
	meta.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	meta.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var parts: Array[String] = []
	var category := str(item.get("category", ""))
	if not category.is_empty():
		parts.append("分类：%s" % category)
	parts.append("重要度 %.2f" % float(item.get("importance", 0.0)))
	if item.has("score"):
		parts.append("匹配度 %s" % str(item.get("score")))
	meta.text = " · ".join(PackedStringArray(parts))
	info.add_child(meta)
	row.add_child(info)
	search_box.add_child(row)

func row_for(memory_id: String) -> Control:
	for row in list_box.get_children():
		if row is Control and str(row.get_meta("memory_id", "")) == memory_id:
			return row
	return null

func _disconnect_service() -> void:
	if _service == null:
		return
	if _service.has_signal("request_finished") and _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.disconnect(_on_request_finished)
	if _service.has_signal("capabilities_received") and _service.capabilities_received.is_connected(_on_capabilities):
		_service.capabilities_received.disconnect(_on_capabilities)

func _ready_for_requests() -> bool:
	return _service != null and _service_state == "ready" and _available

func _feature_available() -> bool:
	if _service == null:
		return false
	var features: Variant = _service.get("features")
	if not features is Dictionary:
		return false
	var feature: Variant = features.get("memory", {})
	return feature is Dictionary and bool(feature.get("available", false))

func _refresh_controls() -> void:
	if refresh_button == null:
		return
	var enabled := _ready_for_requests()
	refresh_button.disabled = not enabled
	compact_button.disabled = not (enabled and _compact_available)
	jobs_button.disabled = not (enabled and _jobs_available)
	rebuild_button.disabled = not (enabled and _index_available)
	index_status_button.disabled = not (enabled and _index_available)
	remember_button.disabled = not (enabled and _propose_available)
	search_button.disabled = not (enabled and _search_available)
	remember_input.editable = enabled and _propose_available
	search_input.editable = enabled and _search_available
	for entry in _action_buttons:
		var action := str(entry.get("action", ""))
		var allowed := enabled
		if action == "history" and not _history_available:
			allowed = false
		elif action == "restore" and not _restore_available:
			allowed = false
		elif action == "correct" and not _correct_available:
			allowed = false
		elif action == "detail" and not _detail_available:
			allowed = false
		elif action == "archive" and not _archive_available:
			allowed = false
		elif action == "resolve" and not _resolve_available:
			allowed = false
		var button: Button = entry.get("button")
		button.disabled = not allowed
	if _service == null:
		status.text = "后台服务未连接。"
	elif _service_state != "ready":
		status.text = "后台服务未连接。"
	elif not _available:
		status.text = UNAVAILABLE_TEXT

func _on_capabilities(_features: Dictionary, _personas: Array) -> void:
	_available = _feature_available()
	_refresh_controls()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _requests.has(id):
		return
	var command := str(_requests[id].get("type", ""))
	var command_memory_id := str(_requests[id].get("memory_id", ""))
	_requests.erase(id)
	var message := str(payload.get("message", ""))
	var code := str(payload.get("code", ""))
	if type == "error" and code in ["not_available", "unknown_command"]:
		_mark_unavailable(command)
		return
	if command == "memory.list":
		if type == "result":
			var items: Variant = payload.get("memories", payload.get("items", []))
			_render(items if items is Array else [])
		elif message.is_empty():
			status.text = "记忆列表加载失败。"
		else:
			status.text = "记忆列表加载失败：%s" % message
		return
	if command == "memory.history":
		if type == "result":
			var versions: Variant = payload.get("versions", payload.get("history", []))
			_render_history(versions if versions is Array else [], command_memory_id)
		elif message.is_empty():
			status.text = "历史版本加载失败。"
			_refresh_controls()
		else:
			status.text = "历史版本加载失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.correct":
		if type == "result":
			status.text = "纠正已保存。"
			refresh()
		elif message.is_empty():
			status.text = "纠正保存失败。"
			_refresh_controls()
		else:
			status.text = "纠正保存失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.restore":
		if type == "result":
			status.text = "记忆已恢复。"
			refresh()
		elif message.is_empty():
			status.text = "恢复失败。"
			_refresh_controls()
		else:
			status.text = "恢复失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.get":
		if type == "result":
			_render_detail(payload, command_memory_id)
		elif message.is_empty():
			status.text = "记忆详情加载失败。"
		else:
			status.text = "记忆详情加载失败：%s" % message
		return
	if command == "memory.archive":
		if type == "result":
			status.text = "记忆已归档。"
			refresh()
		elif message.is_empty():
			status.text = "归档失败。"
			_refresh_controls()
		else:
			status.text = "归档失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.compact":
		if type == "result":
			status.text = "已排队整理 %d 条记忆。" % int(payload.get("queued", 0))
			refresh()
		elif message.is_empty():
			status.text = "整理失败。"
			_refresh_controls()
		else:
			status.text = "整理失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.jobs.status":
		if type == "result":
			status.text = "任务：待处理 %d · 运行中 %d · 失败 %d · 完成 %d%s" % [
				int(payload.get("pending", 0)),
				int(payload.get("running", 0)),
				int(payload.get("failed", 0)),
				int(payload.get("completed", 0)),
				"" if str(payload.get("last_error", "")).is_empty() else " · 最近错误：" + str(payload.get("last_error", "")),
			]
		elif message.is_empty():
			status.text = "任务状态加载失败。"
		else:
			status.text = "任务状态加载失败：%s" % message
		return
	if command == "memory.index.rebuild":
		if type == "result":
			if str(payload.get("status", "")) == "unavailable":
				status.text = "向量索引不可用：%s" % str(payload.get("reason", ""))
			else:
				status.text = "已写入 %d 条向量。" % int(payload.get("queued", 0))
				refresh()
		elif message.is_empty():
			status.text = "索引重建失败。"
			_refresh_controls()
		else:
			status.text = "索引重建失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.index.status":
		if type == "result":
			status.text = "索引：%s · 模型 %s · %d/%d%s" % [
				"已启用" if bool(payload.get("enabled", false)) else "未启用",
				str(payload.get("model", "")),
				int(payload.get("indexed", 0)),
				int(payload.get("total", 0)),
				"" if str(payload.get("last_error", "")).is_empty() else " · 最近错误：" + str(payload.get("last_error", "")),
			]
		elif message.is_empty():
			status.text = "索引状态加载失败。"
		else:
			status.text = "索引状态加载失败：%s" % message
		return
	if command == "memory.conflicts.resolve":
		if type == "result":
			status.text = "冲突已解决，归档 %s。" % str(payload.get("archived", ""))
			refresh()
		elif message.is_empty():
			status.text = "冲突解决失败。"
			_refresh_controls()
		else:
			status.text = "冲突解决失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.conflicts.list":
		if type == "result":
			_render_conflicts(payload.get("conflicts", []))
		else:
			_conflicts_checked = true
		return
	if command == "memory.propose":
		if type == "result":
			var outcome := str(payload.get("status", ""))
			if outcome in ["committed", "merged", "queued"]:
				status.text = "记忆已保存。"
				remember_input.text = ""
				if _searching:
					_clear_search()
				else:
					refresh()
			else:
				status.text = "记录失败：%s" % str(payload.get("reason", ""))
				_refresh_controls()
		elif message.is_empty():
			status.text = "记录失败。"
			_refresh_controls()
		else:
			status.text = "记录失败：%s" % message
			_refresh_controls()
		return
	if command == "memory.search":
		if type == "result":
			var results: Variant = payload.get("results", [])
			_render_search_results(results if results is Array else [])
		elif message.is_empty():
			status.text = "搜索失败。"
			_refresh_controls()
		else:
			status.text = "搜索失败：%s" % message
			_refresh_controls()
		return
	if type == "result":
		refresh()
	elif message.is_empty():
		status.text = "删除失败。"
		_refresh_controls()
	else:
		status.text = "删除失败：%s" % message
		_refresh_controls()

func _mark_unavailable(command: String) -> void:
	if command == "memory.history":
		_history_available = false
		status.text = HISTORY_UNAVAILABLE_TEXT
	elif command == "memory.restore":
		_restore_available = false
		status.text = RESTORE_UNAVAILABLE_TEXT
	elif command == "memory.correct":
		_correct_available = false
		status.text = CORRECT_UNAVAILABLE_TEXT
	elif command == "memory.get":
		_detail_available = false
		status.text = "记忆详情未接入。"
	elif command == "memory.archive":
		_archive_available = false
		status.text = "记忆归档未接入。"
	elif command == "memory.compact":
		_compact_available = false
		status.text = "记忆整理未接入。"
	elif command == "memory.jobs.status":
		_jobs_available = false
		status.text = "记忆任务未接入。"
	elif command in ["memory.index.rebuild", "memory.index.status"]:
		_index_available = false
		status.text = "向量索引未接入。"
	elif command == "memory.conflicts.resolve":
		_resolve_available = false
		status.text = "冲突解决未接入。"
	elif command == "memory.conflicts.list":
		_conflicts_checked = true
	elif command == "memory.propose":
		_propose_available = false
		status.text = PROPOSE_UNAVAILABLE_TEXT
	elif command == "memory.search":
		_search_available = false
		status.text = SEARCH_UNAVAILABLE_TEXT
		_set_searching(false)
		_clear_search_rows()
	else:
		_available = false
		status.text = UNAVAILABLE_TEXT
	_refresh_controls()

func _render(items: Array) -> void:
	for child in list_box.get_children():
		list_box.remove_child(child)
		child.queue_free()
	_rows.clear()
	_action_buttons.clear()
	_conflict_ids.clear()
	_conflicts_checked = false
	if items.is_empty():
		if not _searching:
			status.text = "还没有记忆。"
		_refresh_controls()
		return
	var count := 0
	for item in items:
		if not item is Dictionary:
			continue
		_add_row(item)
		count += 1
	if not _searching:
		status.text = "共 %d 条记忆" % count
	_refresh_controls()
	_request_conflicts()

func _add_row(item: Dictionary) -> void:
	var memory_id := str(item.get("id", ""))
	var text := str(item.get("text", item.get("content", "")))
	if text.is_empty() or memory_id.is_empty():
		return
	var category := str(item.get("category", ""))
	var importance := float(item.get("importance", 0.0))
	var state := str(item.get("status", "active"))
	var row := HBoxContainer.new()
	row.set_meta("memory_id", memory_id)
	var info := VBoxContainer.new()
	info.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	info.add_theme_constant_override("separation", 4)
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	label.text = text
	info.add_child(label)
	var meta_row := HBoxContainer.new()
	var meta := Label.new()
	meta.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	meta.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	meta_row.add_child(meta)
	var detail := Button.new()
	detail.text = "详情"
	detail.pressed.connect(func(): _request_detail(memory_id))
	meta_row.add_child(detail)
	var correct := Button.new()
	correct.text = "纠正"
	correct.pressed.connect(func(): _begin_correct(memory_id, text))
	meta_row.add_child(correct)
	var history := Button.new()
	history.text = "历史"
	history.pressed.connect(func(): _request_history(memory_id))
	meta_row.add_child(history)
	info.add_child(meta_row)
	row.add_child(info)
	var remove := Button.new()
	remove.text = "删除"
	remove.disabled = not _ready_for_requests()
	remove.pressed.connect(func(): _request_delete(memory_id, remove))
	row.add_child(remove)
	var archive: Button = null
	if state in ["active", "superseded", "disputed"]:
		archive = Button.new()
		archive.text = "归档"
		archive.pressed.connect(func(): _request_archive(memory_id, archive))
		row.add_child(archive)
	var restore: Button = null
	if state == "archived":
		restore = Button.new()
		restore.text = "恢复"
		restore.pressed.connect(func(): _request_restore(memory_id, restore))
		row.add_child(restore)
	row.set_meta("detail_button", detail)
	row.set_meta("correct_button", correct)
	row.set_meta("history_button", history)
	row.set_meta("archive_button", archive)
	row.set_meta("restore_button", restore)
	_rows.append({
		"id": memory_id,
		"meta": meta,
		"category": category,
		"importance": importance,
		"state": state,
	})
	_action_buttons.append({"button": correct, "action": "correct"})
	_action_buttons.append({"button": history, "action": "history"})
	_action_buttons.append({"button": detail, "action": "detail"})
	if archive != null:
		_action_buttons.append({"button": archive, "action": "archive"})
	if restore != null:
		_action_buttons.append({"button": restore, "action": "restore"})
	list_box.add_child(row)
	_update_row_meta(_rows[-1])

func _update_row_meta(entry: Dictionary) -> void:
	var parts: Array[String] = []
	var category := str(entry.get("category", ""))
	if not category.is_empty():
		parts.append("分类：%s" % category)
	parts.append("重要度 %.2f" % float(entry.get("importance", 0.0)))
	var state := str(entry.get("state", "active"))
	if state != "active":
		parts.append(_status_label(state))
	if _conflict_ids.has(str(entry.get("id", ""))):
		parts.append("冲突中")
	var meta: Label = entry.get("meta")
	meta.text = " · ".join(PackedStringArray(parts))

func _status_label(state: String) -> String:
	match state:
		"archived":
			return "已归档"
		"superseded":
			return "已替代"
		"pending":
			return "待确认"
		"deleted":
			return "已删除"
		_:
			return state

func _request_conflicts() -> void:
	if _conflicts_checked or not _ready_for_requests():
		return
	_conflicts_checked = true
	var id: String = _service.request("memory.conflicts.list", {}, 10.0)
	if id.is_empty():
		return
	_requests[id] = {"type": "memory.conflicts.list"}

func _render_conflicts(conflicts: Variant) -> void:
	for child in conflicts_box.get_children():
		conflicts_box.remove_child(child)
		child.queue_free()
	_conflict_ids.clear()
	if conflicts is Array:
		for item in conflicts:
			if not item is Dictionary:
				continue
			if str(item.get("status", "")) in ["resolved", "closed"]:
				continue
			for key in ["memory_a", "memory_b"]:
				var memory_id := str(item.get(key, ""))
				if not memory_id.is_empty():
					_conflict_ids[memory_id] = true
			_add_conflict_row(item)
	for entry in _rows:
		_update_row_meta(entry)
	_refresh_controls()
	_conflicts_checked = true

func _add_conflict_row(item: Dictionary) -> void:
	var conflict_id := str(item.get("id", ""))
	if conflict_id.is_empty():
		return
	var row := HBoxContainer.new()
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	label.text = "冲突：%s / %s" % [str(item.get("memory_a", "")), str(item.get("memory_b", ""))]
	row.add_child(label)
	var keep_a := Button.new()
	keep_a.text = "保留 A"
	keep_a.pressed.connect(func(): _request_resolve(conflict_id, "a", keep_a))
	row.add_child(keep_a)
	var keep_b := Button.new()
	keep_b.text = "保留 B"
	keep_b.pressed.connect(func(): _request_resolve(conflict_id, "b", keep_b))
	row.add_child(keep_b)
	conflicts_box.add_child(row)
	_action_buttons.append({"button": keep_a, "action": "resolve"})
	_action_buttons.append({"button": keep_b, "action": "resolve"})

func _request_resolve(conflict_id: String, keep: String, button: Button) -> void:
	if not _ready_for_requests() or not _resolve_available:
		status.text = "冲突解决未接入。"
		return
	button.disabled = true
	var id: String = _service.request("memory.conflicts.resolve", {
		"conflict_id": conflict_id,
		"keep": keep,
	}, 10.0)
	if id.is_empty():
		status.text = "冲突解决失败：后台服务不可用。"
		button.disabled = false
		return
	_requests[id] = {"type": "memory.conflicts.resolve", "conflict_id": conflict_id}
	status.text = "正在解决冲突…"

func _begin_correct(memory_id: String, text: String) -> void:
	if not _ready_for_requests() or not _correct_available:
		status.text = CORRECT_UNAVAILABLE_TEXT
		return
	_correction_memory_id = memory_id
	correction_text.text = text
	correction_dialog.title = "纠正记忆 · %s" % memory_id
	if DisplayServer.get_name() != "headless":
		correction_dialog.popup_centered()

func _submit_correction() -> void:
	if _correction_memory_id.is_empty() or not _ready_for_requests() or not _correct_available:
		return
	var text := correction_text.text.strip_edges()
	if text.is_empty():
		status.text = "纠正内容不能为空。"
		return
	var id: String = _service.request("memory.correct", {
		"memory_id": _correction_memory_id,
		"text": text,
	}, 10.0)
	if id.is_empty():
		status.text = "纠正保存失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.correct"}
	status.text = "正在保存纠正…"
	correction_dialog.hide()

func _request_history(memory_id: String) -> void:
	if not _ready_for_requests() or not _history_available:
		status.text = HISTORY_UNAVAILABLE_TEXT
		return
	var id: String = _service.request("memory.history", {"memory_id": memory_id}, 10.0)
	if id.is_empty():
		status.text = "历史版本加载失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.history", "memory_id": memory_id}
	status.text = "正在加载历史…"

func _render_history(versions: Array, memory_id := "") -> void:
	for child in history_list.get_children():
		history_list.remove_child(child)
		child.queue_free()
	if not memory_id.is_empty():
		history_dialog.title = "记忆历史 · %s" % memory_id
	var index := 0
	if versions.is_empty():
		var empty := Label.new()
		empty.text = "没有历史版本。"
		history_list.add_child(empty)
	else:
		for version in versions:
			if not version is Dictionary:
				continue
			var label := Label.new()
			label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
			var version_id := str(version.get("id", ""))
			var version_status := str(version.get("status", ""))
			var created := str(version.get("created_at", ""))
			var source := str(version.get("source_type", ""))
			var body := str(version.get("text", ""))
			var header := "第 %d 版" % (index + 1)
			if not version_id.is_empty():
				header += " · %s" % version_id
			if not version_status.is_empty():
				header += " · %s" % version_status
			if not created.is_empty():
				header += " · %s" % created
			if not source.is_empty():
				header += " · %s" % source
			label.text = header + "\n" + body
			history_list.add_child(label)
			index += 1
	if DisplayServer.get_name() != "headless":
		history_dialog.popup_centered()
	status.text = "共 %d 个历史版本" % index

func _request_detail(memory_id: String) -> void:
	if not _ready_for_requests() or not _detail_available:
		status.text = "记忆详情未接入。"
		return
	var id: String = _service.request("memory.get", {"memory_id": memory_id}, 10.0)
	if id.is_empty():
		status.text = "记忆详情加载失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.get", "memory_id": memory_id}
	status.text = "正在加载详情…"

func _render_detail(payload: Dictionary, memory_id: String) -> void:
	var memory: Variant = payload.get("memory", {})
	var body := str(payload.get("body", ""))
	if body.is_empty() and memory is Dictionary:
		body = str(memory.get("text", memory.get("content", "")))
	var status_text := ""
	if memory is Dictionary:
		status_text = str(memory.get("status", ""))
	var history: Variant = payload.get("history", [])
	var history_count := 0
	if history is Array:
		history_count = history.size()
	detail_dialog.title = "记忆详情 · %s" % memory_id
	detail_label.text = "状态：%s\n历史版本：%d\n\n%s" % [status_text, history_count, body]
	if DisplayServer.get_name() != "headless":
		detail_dialog.popup_centered()
	status.text = "已加载记忆详情。"

func _request_archive(memory_id: String, button: Button) -> void:
	if not _ready_for_requests() or not _archive_available:
		status.text = "记忆归档未接入。"
		return
	button.disabled = true
	var id: String = _service.request("memory.archive", {"memory_id": memory_id}, 10.0)
	if id.is_empty():
		status.text = "归档失败：后台服务不可用。"
		button.disabled = false
		return
	_requests[id] = {"type": "memory.archive", "memory_id": memory_id}
	status.text = "正在归档…"

func _request_compact() -> void:
	if not _ready_for_requests() or not _compact_available:
		status.text = "记忆整理未接入。"
		return
	var id: String = _service.request("memory.compact", {}, 20.0)
	if id.is_empty():
		status.text = "整理失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.compact"}
	status.text = "正在整理记忆…"

func _request_jobs() -> void:
	if not _ready_for_requests() or not _jobs_available:
		status.text = "记忆任务未接入。"
		return
	var id: String = _service.request("memory.jobs.status", {}, 10.0)
	if id.is_empty():
		status.text = "任务状态加载失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.jobs.status"}
	status.text = "正在读取任务状态…"

func _request_index_rebuild() -> void:
	if not _ready_for_requests() or not _index_available:
		status.text = "向量索引未接入。"
		return
	var id: String = _service.request("memory.index.rebuild", {}, 60.0)
	if id.is_empty():
		status.text = "索引重建失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.index.rebuild"}
	status.text = "正在重建索引…"

func _request_index_status() -> void:
	if not _ready_for_requests() or not _index_available:
		status.text = "向量索引未接入。"
		return
	var id: String = _service.request("memory.index.status", {}, 10.0)
	if id.is_empty():
		status.text = "索引状态加载失败：后台服务不可用。"
		return
	_requests[id] = {"type": "memory.index.status"}
	status.text = "正在读取索引状态…"

func _request_restore(memory_id: String, button: Button) -> void:
	if not _ready_for_requests() or not _restore_available:
		status.text = RESTORE_UNAVAILABLE_TEXT
		return
	button.disabled = true
	var id: String = _service.request("memory.restore", {"memory_id": memory_id}, 10.0)
	if id.is_empty():
		status.text = "恢复失败：后台服务不可用。"
		button.disabled = false
		return
	_requests[id] = {"type": "memory.restore"}
	status.text = "正在恢复…"

func _request_delete(memory_id: String, button: Button) -> void:
	if memory_id.is_empty() or not _ready_for_requests():
		return
	button.disabled = true
	var id: String = _service.request("memory.delete", {"memory_id": memory_id}, 10.0)
	if id.is_empty():
		status.text = "删除失败：后台服务不可用。"
		button.disabled = false
		return
	_requests[id] = {"type": "memory.delete"}
	status.text = "正在删除…"
