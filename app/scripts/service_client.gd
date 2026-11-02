extends Node
## Nonblocking stdio transport. Persistence acknowledgments come from the service.

signal connected(settings: Dictionary, warnings: Array)
signal settings_received(settings: Dictionary)
signal status_changed(state: String, detail: String)
signal request_finished(id: String, type: String, payload: Dictionary)

const Decoder = preload("res://scripts/protocol_decoder.gd")
var state := "stopped"
var detail := "后台服务尚未启动"
var process_pid := 0
var settings := {}
var last_warnings: Array = []
var pending := {}
var _pipe := {}
var _decoder := Decoder.new()
var _executable := ""
var _arguments := PackedStringArray()
var _guard: RefCounted
var _generation := 0
var _sequence := 0
var _restart_count := 0
var _retry_at := 0
var _stopping := false
var _stderr_bytes := 0

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
	pending[id] = {"type": type, "deadline": Time.get_ticks_msec() + int(timeout * 1000)}
	_pipe.stdio.store_buffer(bytes)
	_pipe.stdio.flush()
	if _pipe.stdio.get_error() != OK:
		_failure("向后台服务发送消息失败")
		return ""
	return id

func update_settings(patch: Dictionary) -> String:
	return request("settings.update", patch) if state == "ready" else ""

func retry() -> void:
	if state == "ready" or state == "starting":
		return
	_restart_count = 0
	start()

func _process(_delta: float) -> void:
	if _retry_at > 0 and Time.get_ticks_msec() >= _retry_at and not _stopping:
		_retry_at = 0
		start()
	if process_pid == 0 or _pipe.is_empty():
		return
	if not OS.is_process_running(process_pid):
		_process_exit()
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
	if not OS.is_process_running(process_pid):
		_process_exit()
		return
	var errors_available: int = _pipe.stderr.get_length()
	if errors_available > 0:
		var error_bytes: PackedByteArray = _pipe.stderr.get_buffer(mini(errors_available, 16384))
		if _stderr_bytes < 65536:
			print("[service] ", error_bytes.get_string_from_utf8().strip_edges())
		_stderr_bytes += error_bytes.size()
	if not OS.is_process_running(process_pid):
		_process_exit()
		return
	for id: String in pending.keys():
		if Time.get_ticks_msec() >= int(pending[id].deadline):
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
	if not pending.has(id):
		return # Old generations and cancelled requests never update the UI.
	var command: String = pending[id].type
	pending.erase(id)
	var payload: Dictionary = message.payload
	if command == "hello" and message.type == "ready":
		if payload.get("protocol_version") != 1 or not payload.get("settings") is Dictionary:
			_failure("后台服务握手版本不兼容")
			return
		settings = payload.settings
		last_warnings = payload.get("warnings", []) if payload.get("warnings", []) is Array else []
		_set_status("ready", "后台服务已连接")
		connected.emit(settings, last_warnings)
	elif message.type == "result" and command in ["settings.get", "settings.update"]:
		if not payload.get("settings") is Dictionary:
			_failure("后台服务返回的设置无效")
			return
		settings = payload.settings
		settings_received.emit(settings)
	elif command == "hello" and message.type == "error":
		_failure(str(payload.get("message", "后台服务握手失败")))
		return
	request_finished.emit(id, message.type, payload)

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
		request_finished.emit(id, "error", {"code": "transport_error", "message": message})
	if not _stopping and _restart_count < 1:
		_restart_count += 1
		_retry_at = Time.get_ticks_msec() + 500
		_set_status("restarting", message + "；正在尝试恢复")

func _terminate_owned() -> void:
	if process_pid == 0:
		return
	if _guard:
		_guard.terminate_service_processes()
	elif OS.is_process_running(process_pid):
		OS.kill(process_pid) # Isolated headless tests only, no UI/process discovery.

func _close_pipe() -> void:
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
