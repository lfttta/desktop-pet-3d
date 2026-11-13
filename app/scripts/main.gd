extends "res://scripts/pet_window.gd"

const AppSettings = preload("res://scripts/app_settings.gd")
const ServiceClient = preload("res://scripts/service_client.gd")
const SettingsWindow = preload("res://scripts/ui/settings_window.gd")
const ChatWindow = preload("res://scripts/ui/chat_window.gd")
const EmotionPresenter = preload("res://scripts/state/emotion_presenter.gd")
const TtsPlayer = preload("res://scripts/audio/tts_player.gd")
const Hotkeys = preload("res://scripts/platform/hotkeys.gd")

## Reminders queued while the pet was hidden replay as one bubble. Beyond this
## many the oldest are dropped rather than showing an unreadable wall of text.
const MAX_DEFERRED_TIMER_BUBBLES := 10

## The stock client forwards server-initiated `autonomy.bubble` events but drops
## every other frame that has no pending request. `timer.due` is exactly such a
## frame (the service posts it with the synthetic id "timer"), and so are the
## speech results: `tts.synthesize` answers at once and the service posts
## `tts.ready`/`tts.failed` under the synthesis request id afterwards. This thin
## subclass relays those types to the same `event_received` signal without
## touching the transport's request bookkeeping.
class DesktopServiceClient extends ServiceClient:
	func _dispatch(message: Dictionary) -> void:
		var id := str(message.get("id", ""))
		var known := pending.has(id)
		var payload: Variant = message.get("payload", {})
		super._dispatch(message)
		if known or not payload is Dictionary:
			return
		var type := str(message.get("type", ""))
		if type == "timer.due" or type == "tts.ready" or type == "tts.failed":
			event_received.emit(id, type, payload)

var client: Node
var saved_settings := AppSettings.defaults()
## The service's bubble group. Defaults match service/settings.py so a pre-v7
## service that sends no group still gets the same behaviour.
var bubble := {"enabled": true, "show_replies": true, "duration_seconds": 4}
## The service's greeting group. Defaults match service/settings.py so a pre-v7
## service that sends no group still greets the way it used to.
var greeting_group := {"enabled": true, "on_start": true, "cooldown_minutes": 60}
## The service's voice group. Defaults match service/settings.py, where speech
## synthesis runs off the service loop and speaking is on; the player refuses to
## send anything while enabled is false.
var voice_group := {"enabled": true, "voice": "", "rate": 0, "volume": 70}
var data_directory := ""
var settings_window: Window
var chat_window: Window
var emotion_presenter: Node
var tts_player: Node
var notice: AcceptDialog
var _app_ready := false
var _greeted_on_start := false
var _closing := false
var _applying := false
var _save_due := 0
## Wheel zoom is persisted 300 ms after the last notch, not on every frame.
var _wheel_save_due := 0
var _saving := {}
var _warnings: Array = []
var _last_save_error := ""
## The pair the native layer has actually registered. Empty until the first
## apply, which is also the cue that no fallback exists yet.
var _active_hotkeys: Array = []
## Timer ids already shown, so a repeated server event cannot pop the same
## reminder twice.
##
## The key is `id|due_at`, not the id alone: the timers table's primary key is
## `INTEGER PRIMARY KEY` without AUTOINCREMENT, so after "取消提醒" deletes a row
## the next reminder may reuse its id. Keying on the id alone swallowed the new
## reminder.
var _shown_timer_ids := {}
## Reminder texts that arrived while the pet was hidden. The bubble canvas is
## invisible then (`_bubble_layer.visible` is false), so a bubble shown at that
## moment would expire unseen; the queue replays when the tray shows the pet
## again. `_shown_timer_ids` has already marked the event, so a replayed
## reminder can never pop twice.
var _deferred_timer_bubbles: Array = []
## Ticks of the chat request in flight, or -1 when none. The session's
## `request_sent` starts the clock and every terminal signal (reply, error,
## cancel, unavailable) clears it, so the waiting line measures a real request's
## age instead of counting from a stale send.
var _chat_request_ms := -1
## Whether the in-flight request already had its one waiting attempt. Stopping
## after the first attempt keeps a cooled-down or silenced try from being
## retried — and the phrase file re-parsed — on every frame of a long stream.
var _waiting_announced := false
## Injectable tick source for the waiting measurement, the same pattern as
## pet_window.gd's greeting_clock: left invalid the real clock is used, and a
## test swaps it in so it can move past the waiting threshold without waiting.
var chat_request_clock: Callable = Callable()

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
	client = DesktopServiceClient.new()
	add_child(client)
	# The pet window's fps governor polls desktop attention through this client.
	_desktop_client = client
	client.connected.connect(_on_connected)
	client.settings_received.connect(_on_settings_received)
	client.status_changed.connect(_on_service_status)
	client.request_finished.connect(_on_request_finished)
	client.event_received.connect(_on_service_event)
	var command := _service_command()
	client.configure(command.executable, command.arguments, bridge)
	client.start()
	var deadline := Time.get_ticks_msec() + 8500
	while client.state in ["starting", "restarting"] and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	super._ready()
	# The pet lives on the primary screen only. `super._ready` reads the window's
	# current screen, which is also the primary one at startup; pin it explicitly
	# so a window restored by Windows cannot pick a secondary monitor.
	screen = maxi(0, DisplayServer.get_primary_screen())
	get_window().title = "Pet3D"
	_app_ready = true
	# The service may have delivered settings during the startup wait above;
	# apply the saved behavior and frame caps now that the window exists.
	apply_runtime_settings(saved_settings)
	_restore_placement()
	# The restored window is the only knowledge the motion controller has of
	# where the feet are. Without this bounds sync `anchor` is still (0,0), the
	# ground-line guard in `_begin_walk` compares 0 to 0 and a menu walk drops the
	# pet a full body height the moment it starts.
	_ensure_motion_bounds()
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
	_announce_chat_waiting()
	_poll_wheel()
	_flush_wheel_save()
	if _save_due > 0 and Time.get_ticks_msec() >= _save_due:
		_save_due = 0
		var patch := _placement_patch()
		if not patch.is_empty():
			_save_patch(patch, false)

## Reads the native wheel accumulator once per frame. The bridge reports raw
## WM_MOUSEWHEEL deltas (WHEEL_DELTA = 120 per notch, signed), so a notch is
## 0.05 of scale and a high-resolution wheel can report a fraction.
func _poll_wheel() -> void:
	if bridge == null or not bridge.has_method("poll_wheel_delta"):
		return
	_apply_wheel_delta(int(bridge.poll_wheel_delta()))

## Applies one wheel delta, clamped to the same 0.5..2.0 range as the slider and
## the service validator. Returns the new scale so tests do not depend on window
## sizing (headless does not resize the HWND).
func _apply_wheel_delta(delta: int) -> float:
	if delta == 0:
		return float(saved_settings.get("scale", 1.0))
	var steps := float(delta) / 120.0
	var current := float(saved_settings.get("scale", 1.0))
	var next := clampf(current + steps * 0.05, 0.5, 2.0)
	if is_equal_approx(next, current):
		return current
	saved_settings["scale"] = next
	_resize_role()
	_constrain_window()
	_wheel_save_due = Time.get_ticks_msec() + 300
	return next

## Persists the wheel-selected scale once the 300 ms quiet period has passed.
func _flush_wheel_save(now_ms := -1) -> void:
	if _wheel_save_due <= 0:
		return
	var now := Time.get_ticks_msec() if now_ms < 0 else now_ms
	if now < _wheel_save_due:
		return
	_wheel_save_due = 0
	_save_patch({"scale": float(saved_settings.get("scale", 1.0))}, false)

func _update_input() -> void:
	var was_dragging: bool = drag.active
	super._update_input()
	if was_dragging and not drag.active:
		_queue_position_save()

func _handle_tray(action: int) -> void:
	var was_hidden := hidden
	if action == 6:
		_open_settings()
	elif action == 7:
		_open_chat()
	else:
		super._handle_tray(action)
	# The tray's show/hide toggle is the only path that shows a hidden pet
	# again (the recover command lands here too); reminders that arrived while
	# hidden replay the moment the pet is visible.
	if was_hidden and not hidden:
		_flush_deferred_timer_bubbles()

func _recover_position() -> void:
	super._recover_position()
	if _app_ready:
		_queue_position_save()

func _restore_placement() -> void:
	if DisplayServer.get_name() == "headless":
		return
	# The pet only ever occupies the primary screen's work area. `screen_id` is
	# still accepted in settings documents for compatibility but is never read:
	# a saved placement is a fraction of the primary work area.
	screen = maxi(0, DisplayServer.get_primary_screen())
	var work := _work_area()
	var placement: Dictionary = saved_settings.get("placement", AppSettings.defaults().placement)
	_attached_edges = Vector2i(int(placement.get("edge_x", 1)), int(placement.get("edge_y", 1)))
	var subject := _subject_rect()
	get_window().position = ScreenMath.pin_edges(AppSettings.position_from(placement, subject, work), subject, work, _attached_edges)
	_last_work_area = work

func _check_screen() -> void:
	if not _app_ready:
		return
	if DisplayServer.get_name() != "headless":
		var primary := maxi(0, DisplayServer.get_primary_screen())
		if primary != screen:
			screen = primary
			_cancel_drag()
			_restore_placement()
	if bridge and DisplayServer.get_name() == "Windows":
		var actual_scale: float = bridge.get_dpi_scale()
		if not is_equal_approx(actual_scale, dpi_scale):
			dpi_scale = actual_scale
			_resize_role()
			_restore_placement()
	super._check_screen()

func _placement_patch() -> Dictionary:
	if DisplayServer.get_name() == "headless" or actor == null:
		return {}
	return {"placement": AppSettings.placement(get_window().position, _subject_rect(), _work_area(), _attached_edges)}

func _queue_position_save() -> void:
	if _app_ready and not _closing:
		_save_due = Time.get_ticks_msec() + 300

func _create_windows() -> void:
	settings_window = SettingsWindow.new()
	settings_window.visible = false
	add_child(settings_window)
	settings_window.min_size = Vector2i.ZERO
	settings_window.size = Vector2i(Vector2(560, 440) * dpi_scale)
	settings_window.set_meta("preferred_size", settings_window.size)
	settings_window.visibility_changed.connect(_refresh_fps)
	settings_window.save_requested.connect(_on_save_requested)
	settings_window.recover_requested.connect(func(): _handle_tray(2))
	settings_window.retry_requested.connect(client.retry)
	settings_window.growth_applied.connect(func(): say_celebration())
	chat_window = ChatWindow.new()
	chat_window.visible = false
	add_child(chat_window)
	chat_window.min_size = Vector2i.ZERO
	chat_window.size = Vector2i(Vector2(520, 400) * dpi_scale)
	chat_window.set_meta("preferred_size", chat_window.size)
	chat_window.visibility_changed.connect(_refresh_fps)
	chat_window.retry_requested.connect(client.retry)
	chat_window.configure_chat(client, saved_settings)
	tts_player = TtsPlayer.new()
	add_child(tts_player)
	tts_player.configure(client)
	_connect_chat_bubble()
	_connect_chat_reply()
	_connect_chat_speech()
	_connect_chat_phrases()
	_connect_local_speech()
	_apply_bubble_settings()
	_apply_greeting_settings()
	_apply_voice_settings()
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
## owns one, restores the triple that worked and writes that triple back so the
## file never claims a binding the process does not have.
##
## The native bridge only exists on Windows, so testing the wiring headlessly
## injects a fake bridge instead of checking the platform name here.
func _apply_hotkeys(settings: Dictionary) -> void:
	if bridge == null or not bridge.has_method("configure_hotkeys"):
		return
	var wanted := Hotkeys.bindings(settings)
	if _active_hotkeys == wanted:
		return
	var outcome := Hotkeys.resolve(bridge, str(wanted[0]), str(wanted[1]), _active_hotkeys, str(wanted[2]))
	_active_hotkeys = outcome.active
	if outcome.ok:
		return
	var message := str(outcome.message)
	if settings_window:
		settings_window.show_hotkeys(_active_hotkeys)
		settings_window.show_message(message, true)
	_show_notice("热键没有生效", message)
	if _active_hotkeys.size() >= 2 and _active_hotkeys != wanted and _app_ready and not _closing:
		_save_patch({"hotkeys": _active_hotkeys_patch(_active_hotkeys)}, false)

## The hotkey fields this settings schema can accept. `chat` only exists from
## schema v10; sending it to an older service rejects the whole patch.
func _active_hotkeys_patch(active: Array) -> Dictionary:
	var patch := {"recover": str(active[0]), "exit": str(active[1])}
	if active.size() >= 3 and int(saved_settings.get("schema_version", 0)) >= 10:
		patch["chat"] = str(active[2])
	return patch

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

## Wires the chat session to the pet's app-side phrase pools, so the apology and
## waiting libraries are actually reachable: a failed request lets the pet
## apologize once, and a request still running past the waiting threshold gets
## one waiting line. The pickers in pet_window.gd keep their own cooldowns, so a
## burst of failures cannot spam the bubble.
##
## The apology listens on `error_received`, not `failed`: the session emits
## `failed` alone for local input checks (blank or oversize text) where nothing
## was answered yet, while `error_received` only fires for a request that really
## went out and came back with an error.
func _connect_chat_phrases() -> void:
	if chat_window == null:
		return
	var session: Object = chat_window.get("_session")
	if session == null:
		return
	for pair in [["request_sent", _on_chat_request_sent],
			["error_received", _on_chat_request_failed],
			["assistant_message", _on_chat_reply_settled],
			["cancelled", _on_chat_cancelled],
			["unavailable", _on_chat_unavailable]]:
		if session.has_signal(str(pair[0])) and not session.is_connected(str(pair[0]), pair[1]):
			session.connect(str(pair[0]), pair[1])

## A new request started: restart the waiting measurement.
func _on_chat_request_sent(_id: String, _text: String) -> void:
	_chat_request_ms = _chat_now_ms()
	_waiting_announced = false

## The request failed: settle it and let the pet say an apology. The picker
## checks bubble.enabled and its own cooldown, so a disabled or recently
## apologized pet stays silent instead of repeating itself.
func _on_chat_request_failed(_code: String, _message: String, _retryable: bool) -> void:
	_settle_chat_request()
	say_apology()

func _on_chat_reply_settled(_text: String) -> void:
	_settle_chat_request()

func _on_chat_cancelled() -> void:
	_settle_chat_request()

func _on_chat_unavailable(_message: String) -> void:
	_settle_chat_request()

## Ends the waiting measurement for the current request.
func _settle_chat_request() -> void:
	_chat_request_ms = -1

## Current ticks for the waiting measurement, honouring the injectable clock.
func _chat_now_ms() -> int:
	if chat_request_clock.is_valid():
		return int(chat_request_clock.call())
	return Time.get_ticks_msec()

## Lets the pet acknowledge a chat request that is still streaming past the
## waiting threshold. Runs every frame but does no work while no request is in
## flight or the attempt was made; the picker re-checks the threshold itself.
func _announce_chat_waiting() -> void:
	if _chat_request_ms < 0 or _waiting_announced:
		return
	var elapsed := float(_chat_now_ms() - _chat_request_ms) / 1000.0
	if elapsed < WAITING_PHRASE_SECONDS:
		return
	# One attempt per request, whatever the picker decides: a silenced attempt
	# (cooldown, bubbles off) must not retry the phrase file on every frame.
	_waiting_announced = true
	say_waiting(elapsed)

## Forwards the pet's own click and start-greeting lines to the speech player.
## The player checks voice.enabled itself, so a disabled switch keeps those
## lines bubble-only without extra wiring here.
func _connect_local_speech() -> void:
	if tts_player == null:
		return
	if not local_line_spoken.is_connected(tts_player.speak):
		local_line_spoken.connect(tts_player.speak)

## Copies the voice group from the settings document into the player, so the
## speaking switch follows the service without a restart.
func _apply_voice_settings() -> void:
	var group: Variant = saved_settings.get("voice", {})
	if group is Dictionary:
		for key in voice_group.keys():
			if (group as Dictionary).has(key):
				voice_group[key] = (group as Dictionary)[key]
	if tts_player != null:
		tts_player.set_voice_enabled(bool(voice_group.get("enabled", true)))

## Server-initiated events. `timer.due` arrives once per reminder from the
## service, but a reconnect or a retried transport frame could repeat it; the id
## set keeps the same reminder from popping twice. The text uses the same bubble
## switches and duration as every other desktop bubble.
func _on_service_event(_id: String, type: String, payload: Dictionary) -> void:
	if type != "timer.due":
		return
	var text := str(payload.get("text", "")).strip_edges()
	if text.is_empty():
		return
	var timer_id := str(payload.get("id", ""))
	if not timer_id.is_empty():
		var key := timer_id + "|" + str(payload.get("due_at", ""))
		if _shown_timer_ids.has(key):
			return
		# A long session can see many reminders; drop the history rather than grow
		# without bound. The visible bubble has already expired by then anyway.
		if _shown_timer_ids.size() >= 200:
			_shown_timer_ids.clear()
		_shown_timer_ids[key] = true
	if hidden:
		# While hidden the bubble canvas is invisible, so showing the text now
		# would silently consume the reminder; queue it for the next unhide.
		while _deferred_timer_bubbles.size() >= MAX_DEFERRED_TIMER_BUBBLES:
			_deferred_timer_bubbles.pop_front()
		_deferred_timer_bubbles.append(text)
		return
	show_bubble(text, _bubble_seconds())

## Replays reminders that arrived while the pet was hidden. Everything queued
## goes into one bubble, so a hidden stretch still shows every missed reminder
## exactly once — the docs' "同一个提醒不会重复弹" promise stays true. With
## bubble.enabled off the pet shows nothing, and the queued text is dropped the
## same way an on-screen reminder would be.
func _flush_deferred_timer_bubbles() -> void:
	if hidden or _deferred_timer_bubbles.is_empty():
		return
	var text := "；".join(PackedStringArray(_deferred_timer_bubbles))
	_deferred_timer_bubbles.clear()
	show_bubble(text, _bubble_seconds())

func _open_settings() -> void:
	if settings_window:
		settings_window.populate(saved_settings)
		_place_utility_window(settings_window)
		settings_window.open()
		_refresh_fps()

## Opens the chat window with the caret in the message field. The tray menu and
## the global "open chat" hotkey both land here, as does the reply bar's button.
func _open_chat() -> void:
	if chat_window:
		_place_utility_window(chat_window)
		chat_window.open()
		if chat_window.has_method("focus_input"):
			chat_window.focus_input()
		_refresh_fps()

## Sizes a utility window to the primary work area before centering it. On a
## small screen the design size would otherwise hang off the desktop; the
## content scale shrinks with it, so every control stays reachable.
func _place_utility_window(window: Window, work := Rect2i()) -> void:
	if work.size.x <= 0 or work.size.y <= 0:
		work = _utility_work_area()
	if work.size.x <= 0 or work.size.y <= 0:
		return
	var margin := 16
	var preferred: Vector2i = window.get_meta("preferred_size", window.size)
	var fitted := AppSettings.fit_window_size(preferred, work, margin)
	window.min_size = Vector2i.ZERO
	window.size = fitted
	window.position = AppSettings.utility_position(window.size, work)

## Primary screen work area. Headless tests pin one through `work_area_override`.
func _utility_work_area() -> Rect2i:
	if work_area_override.size.x > 0 and work_area_override.size.y > 0:
		return work_area_override
	return DisplayServer.screen_get_usable_rect(maxi(0, DisplayServer.get_primary_screen()))

## While settings or chat is on screen the pet must not drop to the hidden
## frame cap: those windows are rendered by the same process and would feel
## frozen. The idle cap is the floor, even when hidden_fps is configured higher.
func _hidden_fps_target() -> int:
	if _utility_visible():
		return idle_fps
	return hidden_fps

func _utility_visible() -> bool:
	return (settings_window != null and settings_window.visible) \
		or (chat_window != null and chat_window.visible)

func _refresh_fps() -> void:
	Engine.max_fps = _active_fps()

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
		_restore_placement()
	_applying = false
	apply_runtime_settings(saved_settings)
	_apply_bubble_settings()
	_apply_greeting_settings()
	_apply_voice_settings()
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
