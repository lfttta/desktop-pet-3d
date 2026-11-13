extends "res://scripts/pet_window.gd"

const AppSettings = preload("res://scripts/app_settings.gd")
const ServiceClient = preload("res://scripts/service_client.gd")
const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
const ChatWindow = preload("res://scripts/ui/chat_window.gd")
const EmotionPresenter = preload("res://scripts/state/emotion_presenter.gd")
const TtsPlayer = preload("res://scripts/audio/tts_player.gd")
const Hotkeys = preload("res://scripts/platform/hotkeys.gd")
var client: Node
var saved_settings := AppSettings.defaults()
## The service's bubble group. Defaults match service/settings.py so a pre-v7
## service that sends no group still gets the same behaviour.
var bubble := {"enabled": true, "show_replies": true, "duration_seconds": 4}
## The service's greeting group. Defaults match service/settings.py so a pre-v7
## service that sends no group still greets the way it used to.
var greeting_group := {"enabled": true, "on_start": true, "cooldown_minutes": 60}
var data_directory := ""
var displays: Array = []
var settings_window: Window
var chat_window: Window
var emotion_presenter: Node
var tts_player: Node
var notice: AcceptDialog
var _app_ready := false
var _greeted_on_start := false
var _closing := false
var _applying := false
var _display_fallback := false
var _save_due := 0
var _saving := {}
var _last_display_state := ""
var _warnings: Array = []
var _last_save_error := ""
## The pair the native layer has actually registered. Empty until the first
## apply, which is also the cue that no fallback exists yet.
var _active_hotkeys: Array = []

func _ready() -> void:
	_parse_options()
	if options.has("verify-package"):
		super._ready()
		return
	if options.has("review"):
		set_process(false)
		add_child(load("res://scripts/ui/action_preview.gd").new())
		return
	data_directory = str(options.get("data-dir", OS.get_environment("LOCALAPPDATA").path_join("Pet3D"))).simplify_path().trim_suffix("/")
	if not data_directory.is_absolute_path():
		push_error("Data directory must be absolute")
		get_tree().quit(2)
		return
	if not ClassDB.class_exists("PetWindowBridge"):
		push_error("Stage 1 native library is missing")
		get_tree().quit(3)
		return
	bridge = ClassDB.instantiate("PetWindowBridge")
	if not bridge.has_method("acquire_instance"):
		push_error("Stage 1 native library is out of date")
		get_tree().quit(3)
		return
	var acquired: int = bridge.acquire_instance("Pet3D|" + data_directory.replace("\\", "/").to_lower())
	if acquired != 1:
		print("[app] existing instance notified" if acquired == 0 else "[app] instance ownership failed")
		bridge.shutdown()
		get_tree().quit(0 if acquired == 0 else 3)
		return
	if not _initialize_native():
		return
	client = ServiceClient.new()
	add_child(client)
	client.connected.connect(_on_connected)
	client.settings_received.connect(_on_settings_received)
	client.status_changed.connect(_on_service_status)
	client.request_finished.connect(_on_request_finished)
	var command := _service_command()
	client.configure(command.executable, command.arguments, bridge)
	client.start()
	var deadline := Time.get_ticks_msec() + 8500
	while client.state in ["starting", "restarting"] and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	displays = _read_displays()
	_last_display_state = JSON.stringify(displays)
	_select_display()
	super._ready()
	get_window().title = "Pet3D"
	_app_ready = true
	# The service may have delivered settings during the startup wait above;
	# apply the saved behavior and frame caps now that the window exists.
	apply_runtime_settings(saved_settings)
	_restore_placement()
	_create_windows()
	_apply_hotkeys(saved_settings)
	# Settings may already have arrived during the startup wait, so the ready
	# handshake itself cannot be relied on to trigger the start greeting.
	_greet_on_start_once()
	if not _warnings.is_empty():
		_show_notice("设置已恢复", "\n".join(PackedStringArray(_warnings)))
	if options.has("open-settings"):
		_open_settings()
	print("[app] READY service_pid=", client.process_pid, " data_dir=", data_directory)
	if options.has("report"):
		_write_report()

func _service_command() -> Dictionary:
	var bundled := OS.get_executable_path().get_base_dir().path_join("service/pet-service.exe")
	if FileAccess.file_exists(bundled):
		return {"executable": bundled, "arguments": PackedStringArray(["--data-dir", data_directory])}
	if OS.has_feature("editor"):
		var python := str(options.get("python", OS.get_environment("PET3D_PYTHON")))
		if python.is_empty():
			python = ProjectSettings.globalize_path("res://../.venv/Scripts/python.exe").simplify_path()
		return {"executable": python, "arguments": PackedStringArray(["-u", ProjectSettings.globalize_path("res://../service/main.py").simplify_path(), "--data-dir", data_directory])}
	return {"executable": bundled, "arguments": PackedStringArray(["--data-dir", data_directory])}

func _initialize_native() -> bool:
	if DisplayServer.get_name() != "Windows":
		return true
	if bridge == null or not bridge.configure(DisplayServer.window_get_native_handle(DisplayServer.WINDOW_HANDLE)):
		push_error("Could not initialize Windows controls")
		get_tree().quit(4)
		return false
	bridge.set_interactive(false)
	previous_interactive = false
	dpi_scale = bridge.get_dpi_scale()
	return true

func _build_world() -> void:
	super._build_world()
	_resize_role()

func _load_characters() -> void:
	super._load_characters()
	options["character"] = "0"

func _change_character(index: int) -> void:
	super._change_character(index)
	if is_instance_valid(actor) and not actor.is_connected("visible_bounds_changed", _constrain_window):
		actor.connect("visible_bounds_changed", _constrain_window)
	if emotion_presenter:
		emotion_presenter.apply_now()

func _resize_role() -> void:
	if DisplayServer.get_name() != "headless":
		get_window().size = Vector2i((Vector2(480, 560) * dpi_scale * float(saved_settings.get("scale", 1))).round())

func _process(delta: float) -> void:
	if not _app_ready or _closing:
		return
	super._process(delta)
	if _save_due > 0 and Time.get_ticks_msec() >= _save_due:
		_save_due = 0
		var patch := _placement_patch()
		if not patch.is_empty():
			_save_patch(patch, false)

func _update_input() -> void:
	var was_dragging: bool = drag.active
	super._update_input()
	if was_dragging and not drag.active:
		_queue_position_save()

func _handle_tray(action: int) -> void:
	if action == 6:
		_open_settings()
	elif action == 7:
		_open_chat()
	else:
		super._handle_tray(action)

func _recover_position() -> void:
	super._recover_position()
	if _app_ready:
		_queue_position_save()

func _read_displays() -> Array:
	var result: Variant = JSON.parse_string(bridge.get_displays_json()) if bridge else []
	return result if result is Array else []

func _select_display() -> void:
	var selection := AppSettings.select_display(displays, str(saved_settings.get("screen_id", "")))
	_display_fallback = selection.fallback
	var display: Dictionary = selection.display
	if display.is_empty():
		return
	var position: Array = display.position
	for index in DisplayServer.get_screen_count():
		if DisplayServer.screen_get_position(index) == Vector2i(int(position[0]), int(position[1])):
			screen = index
			return
	screen = maxi(0, DisplayServer.get_primary_screen())

func _restore_placement() -> void:
	_select_display()
	if DisplayServer.get_name() == "headless":
		return
	var work := DisplayServer.screen_get_usable_rect(screen)
	var placement: Dictionary = saved_settings.get("placement", AppSettings.defaults().placement)
	if _display_fallback:
		placement = AppSettings.defaults().placement
	_attached_edges = Vector2i(int(placement.get("edge_x", 1)), int(placement.get("edge_y", 1)))
	var subject := _subject_rect()
	get_window().position = ScreenMath.pin_edges(AppSettings.position_from(placement, subject, work), subject, work, _attached_edges)
	_last_work_area = work

func _check_screen() -> void:
	if not _app_ready:
		return
	var latest := _read_displays()
	var signature := JSON.stringify(latest)
	if signature != _last_display_state:
		_last_display_state = signature
		displays = latest
		_cancel_drag()
		_restore_placement()
		if settings_window and settings_window.visible:
			settings_window.populate(saved_settings, displays)
	if bridge and DisplayServer.get_name() == "Windows":
		var actual_scale: float = bridge.get_dpi_scale()
		if not is_equal_approx(actual_scale, dpi_scale):
			dpi_scale = actual_scale
			_resize_role()
			_restore_placement()
	super._check_screen()

func _placement_patch() -> Dictionary:
	if _display_fallback or DisplayServer.get_name() == "headless" or actor == null:
		return {}
	return {"placement": AppSettings.placement(get_window().position, _subject_rect(), DisplayServer.screen_get_usable_rect(screen), _attached_edges)}

func _queue_position_save() -> void:
	if _app_ready and not _closing:
		_save_due = Time.get_ticks_msec() + 300

func _create_windows() -> void:
	settings_window = SettingsWindow.new()
	settings_window.visible = false
	add_child(settings_window)
	settings_window.size = Vector2i(Vector2(560, 440) * dpi_scale)
	settings_window.save_requested.connect(_on_save_requested)
	settings_window.recover_requested.connect(func(): _handle_tray(2))
	settings_window.retry_requested.connect(client.retry)
	chat_window = ChatWindow.new()
	chat_window.visible = false
	add_child(chat_window)
	chat_window.size = Vector2i(Vector2(520, 400) * dpi_scale)
	chat_window.retry_requested.connect(client.retry)
	chat_window.configure_chat(client, saved_settings)
	tts_player = TtsPlayer.new()
	add_child(tts_player)
	tts_player.configure(client)
	_connect_chat_bubble()
	_connect_chat_reply()
	_connect_chat_speech()
	_apply_bubble_settings()
	_apply_greeting_settings()
	if not bubble_reply_submitted.is_connected(_on_bubble_reply_submitted):
		bubble_reply_submitted.connect(_on_bubble_reply_submitted)
	if not bubble_chat_requested.is_connected(_on_bubble_chat_requested):
		bubble_chat_requested.connect(_on_bubble_chat_requested)
	emotion_presenter = EmotionPresenter.new()
	add_child(emotion_presenter)
	emotion_presenter.configure(client, self)
	chat_window.state_updated.connect(emotion_presenter.apply_delta)
	chat_window.action_requested.connect(_play_chat_action)
	notice = AcceptDialog.new()
	notice.title = "Pet3D"
	add_child(notice)
	_on_service_status(client.state, client.detail)

## Mirrors autonomy bubbles onto the desktop. The chat window keeps its own copy
## in the message list; this only forwards the session signal it already reacts
## to, so both surfaces show the same text.
func _connect_chat_bubble() -> void:
	if chat_window == null:
		return
	var session: Object = chat_window.get("_session")
	if session == null or not session.has_signal("bubble_received"):
		return
	if not session.is_connected("bubble_received", _on_chat_bubble):
		session.connect("bubble_received", _on_chat_bubble)

func _on_chat_bubble(text: String) -> void:
	if not bool(bubble.get("enabled", true)):
		return
	show_bubble(text, _bubble_seconds())

## Adds the reply connection the chat window does not make for the desktop.
func _connect_chat_reply() -> void:
	if chat_window == null:
		return
	var session: Object = chat_window.get("_session")
	if session == null or not session.has_signal("assistant_message"):
		return
	if not session.is_connected("assistant_message", _on_chat_reply):
		session.connect("assistant_message", _on_chat_reply)

## Shows a chat reply on the desktop unless bubble.show_replies is off. The chat
## window keeps the full text; the bubble clips it to forty characters.
func _on_chat_reply(text: String) -> void:
	if not bool(bubble.get("enabled", true)) or not bool(bubble.get("show_replies", true)):
		return
	show_bubble(text, _bubble_seconds(), true)

## Reads the service-configured display time, falling back to the four second
## default when an older settings document has no bubble group.
func _bubble_seconds() -> float:
	return float(bubble.get("duration_seconds", 4))

## Copies the bubble group from the settings document and applies it to the
## window, so enabling or disabling bubbles takes effect without a restart.
func _apply_bubble_settings() -> void:
	var group: Variant = saved_settings.get("bubble", {})
	if group is Dictionary:
		for key in bubble.keys():
			if (group as Dictionary).has(key):
				bubble[key] = (group as Dictionary)[key]
		set_bubble_enabled(bool(bubble.get("enabled", true)))
		set_bubble_seconds(_bubble_seconds())

## Copies the greeting group from the settings document into the window, so the
## start greeting and the click cooldown follow the service without a restart.
func _apply_greeting_settings() -> void:
	var group: Variant = saved_settings.get("greeting", {})
	if group is Dictionary:
		for key in greeting_group.keys():
			if (group as Dictionary).has(key):
				greeting_group[key] = (group as Dictionary)[key]
	set_greeting_settings(
		bool(greeting_group.get("enabled", true)),
		bool(greeting_group.get("on_start", true)),
		int(greeting_group.get("cooldown_minutes", 60)))

## Registers the configured global hotkeys and, when another program already
## owns one, restores the pair that worked and writes that pair back so the
## file never claims a binding the process does not have.
func _apply_hotkeys(settings: Dictionary) -> void:
	if bridge == null or DisplayServer.get_name() != "Windows":
		return
	var wanted := Hotkeys.bindings(settings)
	if _active_hotkeys == wanted:
		return
	var outcome := Hotkeys.resolve(bridge, str(wanted[0]), str(wanted[1]), _active_hotkeys)
	_active_hotkeys = outcome.active
	if outcome.ok:
		return
	var message := str(outcome.message)
	if settings_window:
		settings_window.show_hotkeys(_active_hotkeys)
		settings_window.show_message(message, true)
	_show_notice("热键没有生效", message)
	if _active_hotkeys.size() == 2 and _active_hotkeys != wanted and _app_ready and not _closing:
		_save_patch({"hotkeys": {"recover": _active_hotkeys[0], "exit": _active_hotkeys[1]}}, false)

## Shows the start greeting once, after the client reported ready and the
## settings document has been applied. Deferred so the first frame of the pet is
## drawn before the bubble appears above it.
func _greet_on_start_once() -> void:
	if _greeted_on_start or not _app_ready:
		return
	if client == null or client.state != "ready":
		return
	_greeted_on_start = true
	greet_on_start.call_deferred()

## Sends a line typed into the desktop bubble through the chat window's own
## send path, so history, streaming and error handling stay in one place. When
## chat is not configured the text is kept and the window opens to explain why.
func _on_bubble_reply_submitted(text: String) -> void:
	if chat_window == null:
		return
	if not chat_window.submit_text(text):
		_open_chat()

## The reply bar's "聊天" button: open the window with the caret ready.
func _on_bubble_chat_requested() -> void:
	_open_chat()
	if chat_window and chat_window.has_method("focus_input"):
		chat_window.focus_input()

## Forwards the reply's spoken line to the speech player. The chat window owns
## the session; this only adds the one connection it does not react to itself.
func _connect_chat_speech() -> void:
	if chat_window == null or tts_player == null:
		return
	var session: Object = chat_window.get("_session")
	if session == null or not session.has_signal("speech_requested"):
		return
	if not session.is_connected("speech_requested", tts_player.speak):
		session.connect("speech_requested", tts_player.speak)

func _open_settings() -> void:
	if settings_window:
		settings_window.populate(saved_settings, displays)
		_place_utility_window(settings_window)
		settings_window.open()

func _open_chat() -> void:
	if chat_window:
		_place_utility_window(chat_window)
		chat_window.open()

func _place_utility_window(window: Window) -> void:
	var target_screen := clampi(screen, 0, maxi(0, DisplayServer.get_screen_count() - 1))
	var work := DisplayServer.screen_get_usable_rect(target_screen)
	window.current_screen = target_screen
	window.position = AppSettings.utility_position(window.size, work)

func _on_save_requested(patch: Dictionary) -> void:
	_save_due = 0
	patch.merge(_placement_patch())
	_save_patch(patch, true)

func _save_patch(patch: Dictionary, user_initiated: bool) -> String:
	if client == null or client.state != "ready":
		var previous_error := _last_save_error
		_last_save_error = "后台服务未连接，设置尚未保存。角色仍可使用。"
		if settings_window:
			settings_window.show_message(_last_save_error, true)
		if not _closing and previous_error != _last_save_error:
			_show_notice("位置尚未保存", _last_save_error)
		return ""
	var id: String = client.update_settings(patch)
	if id.is_empty():
		_last_save_error = "发送失败，设置尚未保存"
		if settings_window:
			settings_window.show_message(_last_save_error, true)
		return ""
	_saving[id] = user_initiated
	if settings_window and user_initiated:
		settings_window.show_message("正在保存…")
	return id

func _on_connected(settings: Dictionary, warnings: Array) -> void:
	_warnings = warnings
	_on_settings_received(settings)
	_greet_on_start_once()
	if _app_ready and options.has("report"):
		_write_report()

func _on_settings_received(settings: Dictionary) -> void:
	var previous := saved_settings
	saved_settings = settings.duplicate(true)
	if not _app_ready:
		return
	_applying = true
	if previous.get("scale") != saved_settings.scale:
		_resize_role()
	if previous.get("scale") != saved_settings.scale or previous.get("screen_id") != saved_settings.screen_id:
		_restore_placement()
	_applying = false
	apply_runtime_settings(saved_settings)
	_apply_bubble_settings()
	_apply_greeting_settings()
	_apply_hotkeys(saved_settings)
	if chat_window:
		chat_window.update_settings(saved_settings)
	if emotion_presenter:
		emotion_presenter.apply_now()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if not _saving.has(id):
		return
	var user_initiated: bool = _saving[id]
	_saving.erase(id)
	if type == "result":
		_last_save_error = ""
		if settings_window:
			settings_window.show_message("设置已保存" if user_initiated else "位置已保存")
	else:
		_last_save_error = "保存失败：" + str(payload.get("message", "后台服务不可用"))
		if settings_window:
			settings_window.show_message(_last_save_error, true)
		if user_initiated or not _closing:
			_show_notice("设置尚未保存", _last_save_error)

func _on_service_status(state: String, detail: String) -> void:
	print("[app] service state=", state, " pid=", client.process_pid if client else 0, " detail=", detail)
	if settings_window:
		settings_window.show_status(state, detail)
	if chat_window:
		chat_window.show_status(detail)
	if _app_ready and options.has("report"):
		_write_report()

func _show_notice(title_: String, text: String) -> void:
	if notice and DisplayServer.get_name() != "headless":
		notice.title = title_
		notice.dialog_text = text
		notice.popup_centered(Vector2i(480, 180))
	else:
		print("[app] ", title_, ": ", text)

func _status() -> Dictionary:
	var data := super._status()
	data["stage"] = 2
	data["data_directory"] = data_directory
	data["service_pid"] = client.process_pid if client else 0
	data["service_state"] = client.state if client else "stopped"
	data["saved_settings"] = saved_settings
	data["display_fallback"] = _display_fallback
	data["last_save_error"] = _last_save_error
	data["warnings"] = _warnings
	return data

func _quit(code: int = 0) -> void:
	if _closing:
		return
	_closing = true
	if is_instance_valid(actor):
		actor.reset_performance_state()
	_save_due = 0
	_cancel_drag()
	if client:
		if _app_ready and client.state == "ready":
			var patch := _placement_patch()
			if not patch.is_empty():
				_save_patch(patch, false)
			var deadline := Time.get_ticks_msec() + 1800
			while not _saving.is_empty() and Time.get_ticks_msec() < deadline:
				await get_tree().process_frame
		await client.stop(1.5)
	super._quit(code)
