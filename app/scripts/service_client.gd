extends Node
## Nonblocking stdio transport. Persistence acknowledgments come from the service.
##
## Writes go through a dedicated writer thread: a pipe whose reader stopped
## consuming would otherwise block `store_buffer` on the main thread and freeze
## the whole UI. Reads stay on the main thread. Both directions reach the same
## FileAccess object but through different methods (`store_buffer`/`flush`/
## `get_error` on the writer, `get_length`/`get_buffer` on the reader), and only
## the writer thread ever writes, so the pipe's write end has a single user.

signal connected(settings: Dictionary, warnings: Array)
signal settings_received(settings: Dictionary)
signal status_changed(state: String, detail: String)
signal request_finished(id: String, type: String, payload: Dictionary)
signal event_received(id: String, type: String, payload: Dictionary)
signal capabilities_received(features: Dictionary, personas: Array)

const Decoder = preload("res://scripts/protocol_decoder.gd")
const STREAM_TIMEOUT_SECONDS := 40.0
const STREAM_TIMEOUT_MS := 40000
const TERMINAL_EVENTS := ["chat.done", "chat.error"]
# A stalled generation or a slow periodic poll must not tear down the whole
# transport: those requests fail locally and the service keeps running. Other
# commands keep the older behaviour where a timeout is a transport failure.
const LOCAL_TIMEOUT_COMMANDS := ["chat.send", "state.get", "desktop.status"]
## Persistent auto-reconnect: 1s, 2s, 4s … capped at 30s. A successful handshake
## resets the backoff, so a service that crashes repeatedly is always retried
## instead of leaving the pet inert after a single attempt.
const RESTART_BASE_DELAY := 1.0
const RESTART_MAX_DELAY := 30.0
## Liveness is checked every frame: reading a pipe whose child already exited
## makes Godot log a PeekNamedPipe error, and a once-per-second cache left a
## one-second window of exactly that. One OpenProcess/GetExitCodeProcess per
## frame is far cheaper than the pipe reads it guards.
var state := "stopped"
var detail := "后台服务尚未启动"
var process_pid := 0
var settings := {}
var features := {}
var personas: Array = []
var last_warnings: Array = []
var pending := {}
var _pipe := {}
var _decoder := Decoder.new()
var _executable := ""
var _arguments := PackedStringArray()
var _guard: RefCounted
var _generation := 0
var _sequence := 0
var _restart_delay := RESTART_BASE_DELAY
var _retry_at := 0
var _stopping := false
var _stderr_bytes := 0
var _liveness := true
## Outbound queue drained by the writer thread. Ordering is preserved by the
## single queue + single writer; every chunk is flushed as soon as it is written.
var _outbound: Array[PackedByteArray] = []
var _outbound_mutex := Mutex.new()
var _outbound_ready := Semaphore.new()
var _writer_thread: Thread
var _writer_stdio: FileAccess
var _writer_stop := false
var _writer_failed := false

func configure(executable: String, arguments: PackedStringArray, guard: RefCounted = null) -> void:
	_executable = executable
	_arguments = arguments
	_guard = guard
	process_mode = Node.PROCESS_MODE_ALWAYS

func start() -> bool:
	if process_pid != 0:
		return false
	_stopping = false
	_retry_at = 0
	_decoder = Decoder.new()
	_generation += 1
	_stderr_bytes = 0
	_set_status("starting", "正在连接后台服务…")
	_pipe = OS.execute_with_pipe(_executable, _arguments, false)
	if _pipe.is_empty() or not _pipe.has("stdio") or not _pipe.has("stderr"):
		_failure("无法启动后台服务，请检查程序目录是否完整")
		return false
	process_pid = int(_pipe.pid)
	if _guard and not _guard.adopt_service_process(process_pid):
		# This is the exact PID just returned by our own process creation.
		OS.kill(process_pid)
		_close_pipe()
		_failure("无法建立后台进程回收保护")
		return false
	_liveness = true
	_start_writer()
	request("hello", {"protocol_version": 1}, 8.0)
	return true

func request(type: String, payload: Dictionary, timeout := 8.0) -> String:
	if process_pid == 0 or _pipe.is_empty():
		return ""
	_sequence += 1
	var id := "g%d-r%d" % [_generation, _sequence]
	var bytes := (JSON.stringify({"version": 1, "id": id, "type": type, "payload": payload}) + "\n").to_utf8_buffer()
	if bytes.size() > 1048576:
		return ""
	var effective := timeout
	if type == "chat.send":
		effective = maxf(effective, STREAM_TIMEOUT_SECONDS)
	pending[id] = {
		"type": type,
		"token": id,
		"stream_id": "",
		"deadline": Time.get_ticks_msec() + int(effective * 1000),
		"timeout_ms": int(effective * 1000),
	}
	if not _enqueue(bytes):
		pending.erase(id)
		return ""
	return id

## Hands one frame to the writer thread. A dead writer (broken pipe) refuses the
## frame; the main loop notices the failure and tears the transport down.
func _enqueue(bytes: PackedByteArray) -> bool:
	_outbound_mutex.lock()
	var healthy := not _writer_stop and not _writer_failed and _writer_thread != null
	if healthy:
		_outbound.append(bytes)
	_outbound_mutex.unlock()
	if healthy:
		_outbound_ready.post()
	return healthy

func _start_writer() -> void:
	_outbound_mutex.lock()
	_outbound.clear()
	_writer_stop = false
	_writer_failed = false
	_outbound_mutex.unlock()
	_writer_stdio = _pipe.stdio
	_writer_thread = Thread.new()
	_writer_thread.start(_writer_loop)

## The only place that writes to the pipe. A full pipe blocks this thread, never
## the UI; killing the child breaks the pipe and unblocks it for shutdown.
func _writer_loop() -> void:
	while true:
		_outbound_ready.wait()
		_outbound_mutex.lock()
		var stopping := _writer_stop
		var chunk := PackedByteArray()
		if not stopping and not _outbound.is_empty():
			chunk = _outbound.pop_front()
		_outbound_mutex.unlock()
		if stopping:
			return
		if chunk.is_empty():
			continue
		_writer_stdio.store_buffer(chunk)
		_writer_stdio.flush()
		if _writer_stdio.get_error() != OK:
			_outbound_mutex.lock()
			_writer_failed = true
			_outbound_mutex.unlock()
			return

func _writer_has_failed() -> bool:
	_outbound_mutex.lock()
	var failed := _writer_failed
	_outbound_mutex.unlock()
	return failed

## Joins the writer before the pipe handles close. Callers must terminate the
## child first: a writer blocked on a full pipe only unblocks once the reading
## end of the pipe is gone.
func _stop_writer() -> void:
	if _writer_thread == null:
		return
	_outbound_mutex.lock()
	_writer_stop = true
	_outbound.clear()
	_outbound_mutex.unlock()
	_outbound_ready.post()
	_writer_thread.wait_to_finish()
	_writer_thread = null
	_writer_stdio = null

func update_settings(patch: Dictionary) -> String:
	return request("settings.update", patch) if state == "ready" else ""

func cancel(id: String) -> String:
	if not pending.has(id):
		return ""
	var entry: Dictionary = pending[id]
	var target := str(entry.get("stream_id", ""))
	if target.is_empty():
		target = id
	var cancel_id := request("chat.cancel", {"request_id": target}, 8.0)
	_drop_pending(id)
	return cancel_id

func feature(name: String) -> Dictionary:
	var value: Variant = features.get(name, {})
	if value is Dictionary:
		return value
	return {}

func retry() -> void:
	if state == "ready" or state == "starting":
		return
	_restart_delay = RESTART_BASE_DELAY
	_retry_at = 0
	start()

func _process(_delta: float) -> void:
	if _retry_at > 0 and Time.get_ticks_msec() >= _retry_at and not _stopping:
		_retry_at = 0
		start()
	if process_pid == 0 or _pipe.is_empty():
		return
	var now := Time.get_ticks_msec()
	# Always re-check before touching the pipes: a cached "alive" from up to a
	# second ago would read a dead pipe and log an engine error.
	_liveness = OS.is_process_running(process_pid)
	if not _liveness:
		_process_exit()
		return
	if _writer_has_failed():
		_failure("向后台服务发送消息失败")
		return
	var available: int = _pipe.stdio.get_length()
	if available > 0:
		var decoded: Dictionary = _decoder.feed(_pipe.stdio.get_buffer(mini(available, 65536)))
		if not str(decoded.error).is_empty():
			_failure(decoded.error)
			return
		for message: Dictionary in decoded.messages:
			_dispatch(message)
			if process_pid == 0:
				return
	var errors_available: int = _pipe.stderr.get_length()
	if errors_available > 0:
		var error_bytes: PackedByteArray = _pipe.stderr.get_buffer(mini(errors_available, 16384))
		if _stderr_bytes < 65536:
			print("[service] ", error_bytes.get_string_from_utf8().strip_edges())
		_stderr_bytes += error_bytes.size()
	_expire_requests(now)

## Fail every request whose deadline has passed.
##
## Chat generations and periodic polls fail locally so one slow request cannot
## tear down a healthy backend; other commands still treat a timeout as a
## transport failure.
func _expire_requests(now: int) -> void:
	var expired: Array = []
	for id: String in pending.keys():
		if now >= int(pending[id].deadline):
			expired.append(id)
	for id: String in expired:
		if not pending.has(id):
			continue
		var expired_type := str(pending[id].get("type", ""))
		if LOCAL_TIMEOUT_COMMANDS.has(expired_type):
			if expired_type == "chat.send":
				var stream_id := str(pending[id].get("stream_id", ""))
				if not stream_id.is_empty():
					request("chat.cancel", {"request_id": stream_id}, 8.0)
			_drop_pending(id)
			request_finished.emit(id, "error", {
				"code": "timeout",
				"message": "聊天响应超时，请重试。" if expired_type == "chat.send" else "后台服务响应超时，请重试。",
				"retryable": true,
			})
			continue
		_failure("后台服务响应超时")
		return

func _process_exit() -> void:
	if _stopping:
		_close_pipe()
		_set_status("stopped", "后台服务已退出")
	else:
		_failure("后台服务意外退出，角色仍可继续使用")

func _dispatch(message: Dictionary) -> void:
	var id: String = message.id
	if id.is_empty() and message.type == "error" and state != "ready":
		_failure(str(message.payload.get("message", "后台数据目录不可用")))
		return
	var payload: Dictionary = message.payload
	if not pending.has(id):
		# Server-initiated autonomy bubbles have no pending request to match.
		if message.type == "autonomy.bubble":
			event_received.emit(id, message.type, payload)
		return # Old generations and cancelled requests never update the UI.
	var command: String = pending[id].type

	if command == "hello" and message.type == "ready":
		_drop_pending(id)
		if payload.get("protocol_version") != 1 or not payload.get("settings") is Dictionary:
			_failure("后台服务握手版本不兼容")
			return
		settings = payload.settings
		features = payload.get("features", {}) if payload.get("features") is Dictionary else {}
		personas = payload.get("personas", []) if payload.get("personas") is Array else []
		last_warnings = payload.get("warnings", []) if payload.get("warnings") is Array else []
		# A healthy handshake earns the full retry budget back.
		_restart_delay = RESTART_BASE_DELAY
		_set_status("ready", "后台服务已连接")
		connected.emit(settings, last_warnings)
		capabilities_received.emit(features, personas)
		request_finished.emit(id, message.type, payload)
		return

	if message.type == "result" or message.type == "error":
		if command == "hello" and message.type == "error":
			_drop_pending(id)
			_failure(str(payload.get("message", "后台服务握手失败")))
			return
		if command == "chat.send" and message.type == "result" and payload.has("request_id") and not payload.has("message"):
			# Accepted: generation continues under a stream id until a terminal event.
			var stream_id := str(payload.get("request_id", ""))
			if not stream_id.is_empty() and stream_id != id:
				_alias_pending(id, stream_id)
			_extend_deadline(id)
			event_received.emit(id, "accepted", payload)
			request_finished.emit(id, message.type, payload)
			return
		_drop_pending(id)
		if message.type == "result" and command in ["settings.get", "settings.update"]:
			if not payload.get("settings") is Dictionary:
				_failure("后台服务返回的设置无效")
				return
			settings = payload.settings
			settings_received.emit(settings)
		request_finished.emit(id, message.type, payload)
		return

	# Chat generation is keyed by the accepted request id, not the transport id.
	if message.type == "accepted":
		var stream_id := str(payload.get("request_id", ""))
		if not stream_id.is_empty() and stream_id != id:
			_alias_pending(id, stream_id)
		_extend_deadline(id)
		event_received.emit(id, message.type, payload)
		return

	# Async event frames keep the originating request open until a terminal event.
	event_received.emit(id, message.type, payload)
	if message.type in TERMINAL_EVENTS:
		_drop_pending(id)
		request_finished.emit(id, message.type, payload)
	else:
		_extend_deadline(id)

func _alias_pending(id: String, alias: String) -> void:
	if not pending.has(id):
		return
	var entry: Dictionary = pending[id]
	entry["stream_id"] = alias
	pending[alias] = entry

func _extend_deadline(id: String) -> void:
	if not pending.has(id):
		return
	var entry: Dictionary = pending[id]
	var span := int(entry.get("timeout_ms", STREAM_TIMEOUT_MS))
	entry["deadline"] = Time.get_ticks_msec() + span

func _drop_pending(id: String) -> void:
	if not pending.has(id):
		return
	var token := str(pending[id].get("token", id))
	for key: String in pending.keys():
		if str(pending[key].get("token", "")) == token:
			pending.erase(key)

func stop(timeout := 2.0) -> void:
	_stopping = true
	_retry_at = 0
	if process_pid != 0:
		request("shutdown", {}, timeout)
		var deadline := Time.get_ticks_msec() + int(timeout * 1000)
		while process_pid != 0 and Time.get_ticks_msec() < deadline:
			await get_tree().process_frame
		if process_pid != 0:
			_terminate_owned()
			_close_pipe()
	pending.clear()
	_set_status("stopped", "后台服务已退出")

func _failure(message: String) -> void:
	_terminate_owned()
	_close_pipe()
	var abandoned := pending.keys()
	pending.clear()
	_set_status("failed", message)
	for id: String in abandoned:
		request_finished.emit(id, "error", {"code": "transport_error", "message": message, "retryable": true})
	if not _stopping:
		# Persistent reconnect with capped exponential backoff. The state signal
		# carries the wait so the UI can show a reconnecting status.
		_retry_at = Time.get_ticks_msec() + int(_restart_delay * 1000)
		_set_status("restarting", message + "；%d 秒后自动重连" % int(_restart_delay))
		_restart_delay = minf(_restart_delay * 2.0, RESTART_MAX_DELAY)

func _terminate_owned() -> void:
	if process_pid == 0:
		return
	if _guard:
		_guard.terminate_service_processes()
	elif OS.is_process_running(process_pid):
		OS.kill(process_pid) # Isolated headless tests only, no UI/process discovery.

func _close_pipe() -> void:
	_stop_writer()
	for key in ["stdio", "stderr"]:
		if _pipe.has(key):
			_pipe[key].close()
	_pipe.clear()
	process_pid = 0

func _set_status(next: String, message: String) -> void:
	state = next
	detail = message
	status_changed.emit(state, detail)

func _exit_tree() -> void:
	_stopping = true
	_terminate_owned()
	_close_pipe()
