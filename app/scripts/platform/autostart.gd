extends RefCounted
## Start-with-Windows support without native code.
##
## The entry lives in HKCU (never needs elevation) and is written with the
## built-in `reg` command, so the setting works in a packaged build without a
## new bridge export. Only a packaged Windows build may write it: in the editor
## the executable path is the Godot binary, and a stale entry would start the
## engine instead of the pet.

const RUN_KEY := "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"
const VALUE_NAME := "Pet3D"

## Test hook: point the module at a throwaway key so an integration check can
## exercise `reg` without touching the real startup entry.
static var key_override := ""


static func run_key() -> String:
	return key_override if not key_override.is_empty() else RUN_KEY


## True only where the entry would point at the shipped executable.
static func supported() -> bool:
	return OS.get_name() == "Windows" and (not OS.has_feature("editor")
		or not key_override.is_empty())


## Quoted form the Run key needs when the path contains spaces.
static func command_for(path: String) -> String:
	return '"' + path + '"'


static func delete_arguments() -> PackedStringArray:
	return PackedStringArray(["delete", run_key(), "/v", VALUE_NAME, "/f"])


static func query_arguments() -> PackedStringArray:
	return PackedStringArray(["query", run_key(), "/v", VALUE_NAME])


## The stored command for this executable, or "" when the value is absent.
static func stored_command() -> String:
	if not supported():
		return ""
	var output := []
	var code := OS.execute("reg", query_arguments(), output, true)
	if code != 0:
		return ""
	# `reg query` prints "    Pet3D    REG_SZ    "C:\path\Pet3D.exe""; OS.execute
	# may hand the whole output back as one chunk, so split every chunk itself.
	for chunk in output:
		for raw in str(chunk).split("\n"):
			var text := raw.strip_edges()
			if not text.begins_with(VALUE_NAME):
				continue
			var marker := text.find("REG_SZ")
			if marker < 0:
				continue
			return text.substr(marker + 6).strip_edges()
	return ""


## True when the Run entry points at this executable. A stale entry for an old
## install path reads as off, so the user can repair it by switching it on.
static func is_enabled() -> bool:
	var stored := stored_command()
	if stored.is_empty():
		return false
	return stored.trim_prefix("\"").trim_suffix("\"") == OS.get_executable_path()


## Adds or removes the entry. Returns false when `reg` failed; the caller shows
## the outcome instead of silently pretending the toggle worked.
static func set_enabled(enabled: bool) -> bool:
	if not supported():
		return false
	if not enabled:
		var removed := []
		return OS.execute("reg", delete_arguments(), removed, true) == 0
	# `reg add /d "quoted path"` cannot be passed through OS.execute: arguments
	# containing spaces are quoted again and reg reports invalid syntax. A .reg
	# file carries the nested quotes verbatim.
	var path := _reg_file_path()
	if path.is_empty() or not _write_reg_file(path):
		return false
	var imported := []
	var code := OS.execute("reg", PackedStringArray(["import", path]), imported, true)
	DirAccess.remove_absolute(path)
	return code == 0


static func _reg_file_path() -> String:
	var directory := OS.get_environment("TEMP")
	if directory.is_empty():
		directory = OS.get_cache_dir()
	if directory.is_empty():
		return ""
	return directory.path_join("pet3d-autostart-%d.reg" % OS.get_process_id())


## `.reg` key paths use the long hive name; value quotes are escaped with `\"`.
static func reg_file_body() -> String:
	var key := run_key()
	if key.begins_with("HKCU\\"):
		key = "HKEY_CURRENT_USER\\" + key.substr(5)
	var path := OS.get_executable_path().replace("\\", "\\\\")
	var inner := "\\\"" + path + "\\\""
	return "Windows Registry Editor Version 5.00\r\n\r\n[" + key + "]\r\n\"" \
		+ VALUE_NAME + "\"=\"" + inner + "\"\r\n"


static func _write_reg_file(path: String) -> bool:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	# reg.exe wants UTF-16 with a byte-order mark for version 5 files.
	file.store_8(0xFF)
	file.store_8(0xFE)
	file.store_buffer(reg_file_body().to_utf16_buffer())
	file.close()
	return true
