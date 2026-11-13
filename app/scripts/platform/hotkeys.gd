extends RefCounted
## Global hotkey text parsing and registration.
##
## The settings file stores bindings as "Ctrl+Alt+F10" style text so they stay
## readable and editable. Native `configure_hotkeys` takes Win32 modifier bits
## and virtual-key codes in one "mods,vk,mods,vk" string, which this module
## builds. Registration is process-wide, so a failure means another program
## already owns the combination; that outcome is reported, never hidden.

const MOD_ALT := 0x0001
const MOD_CONTROL := 0x0002
const MOD_SHIFT := 0x0004
const MOD_WIN := 0x0008

const DEFAULT_RECOVER := "Ctrl+Alt+F10"
const DEFAULT_EXIT := "Ctrl+Alt+Q"

const MODIFIER_ALIASES := {
	"ctrl": MOD_CONTROL, "control": MOD_CONTROL,
	"alt": MOD_ALT, "option": MOD_ALT,
	"shift": MOD_SHIFT,
	"win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}
## Canonical modifier order in the stored text.
const MODIFIER_ORDER := [
	["Ctrl", MOD_CONTROL],
	["Alt", MOD_ALT],
	["Shift", MOD_SHIFT],
	["Win", MOD_WIN],
]
## Named keys, keyed by the lowercase alias a user may type.
const NAMED_KEYS := {
	"space": ["Space", 0x20], "spacebar": ["Space", 0x20],
	"tab": ["Tab", 0x09],
	"enter": ["Enter", 0x0D], "return": ["Enter", 0x0D],
	"esc": ["Esc", 0x1B], "escape": ["Esc", 0x1B],
	"backspace": ["Backspace", 0x08],
	"delete": ["Delete", 0x2E], "del": ["Delete", 0x2E],
	"insert": ["Insert", 0x2D], "ins": ["Insert", 0x2D],
	"home": ["Home", 0x24], "end": ["End", 0x23],
	"pageup": ["PageUp", 0x21], "pgup": ["PageUp", 0x21],
	"pagedown": ["PageDown", 0x22], "pgdn": ["PageDown", 0x22],
	"up": ["Up", 0x26], "down": ["Down", 0x28],
	"left": ["Left", 0x25], "right": ["Right", 0x27],
	"pause": ["Pause", 0x13],
	"printscreen": ["PrintScreen", 0x2C], "prtsc": ["PrintScreen", 0x2C],
	"capslock": ["CapsLock", 0x14],
}


static func defaults() -> Array:
	return [DEFAULT_RECOVER, DEFAULT_EXIT]


## Reads the pair out of a settings document, falling back per binding.
static func bindings(settings: Dictionary) -> Array:
	var group: Variant = settings.get("hotkeys", {})
	if not group is Dictionary:
		group = {}
	return [str(group.get("recover", DEFAULT_RECOVER)), str(group.get("exit", DEFAULT_EXIT))]


## Parses one binding. Returns {ok, mods, vk, canonical, error}; on failure only
## `ok` and `error` are meaningful.
static func parse(spec: String) -> Dictionary:
	var text := spec.strip_edges()
	if text.is_empty():
		return {"ok": false, "error": "热键不能为空"}
	var parts := text.split("+", false)
	if parts.is_empty():
		return {"ok": false, "error": "热键格式应为 Ctrl+Alt+Q"}
	var mods := 0
	var seen := {}
	var key_label := ""
	var vk := 0
	for raw in parts:
		var token := str(raw).strip_edges().to_lower()
		if token.is_empty():
			return {"ok": false, "error": "热键里有多余的加号"}
		if MODIFIER_ALIASES.has(token):
			var bit: int = MODIFIER_ALIASES[token]
			if seen.has(bit):
				return {"ok": false, "error": "修饰键重复"}
			seen[bit] = true
			mods |= bit
			continue
		if not key_label.is_empty():
			return {"ok": false, "error": "只能有一个主键"}
		var resolved := _resolve_key(token)
		if resolved.is_empty():
			return {"ok": false, "error": "认不出这个键：" + str(raw).strip_edges()}
		key_label = str(resolved.label)
		vk = int(resolved.vk)
	if key_label.is_empty():
		return {"ok": false, "error": "缺少主键，例如 Ctrl+Alt+Q"}
	if mods == 0:
		# Without a modifier the binding would swallow ordinary typing.
		return {"ok": false, "error": "至少要带一个修饰键（Ctrl / Alt / Shift / Win）"}
	return {"ok": true, "mods": mods, "vk": vk,
		"canonical": format_binding(mods, key_label), "error": ""}


static func _resolve_key(token: String) -> Dictionary:
	if token.length() == 1:
		var code := token.unicode_at(0)
		if code >= 0x61 and code <= 0x7A:
			return {"label": token.to_upper(), "vk": code - 0x20}
		if code >= 0x30 and code <= 0x39:
			return {"label": token, "vk": code}
	if token.length() >= 2 and token[0] == "f":
		var number := token.substr(1)
		if number.is_valid_int() and int(number) >= 1 and int(number) <= 24:
			return {"label": "F%d" % int(number), "vk": 0x70 + int(number) - 1}
	if NAMED_KEYS.has(token):
		var entry: Array = NAMED_KEYS[token]
		return {"label": entry[0], "vk": entry[1]}
	return {}


static func format_binding(mods: int, key_label: String) -> String:
	var pieces: Array[String] = []
	for entry in MODIFIER_ORDER:
		if mods & int(entry[1]):
			pieces.append(str(entry[0]))
	pieces.append(key_label)
	return "+".join(pieces)


static func encode(recover: Dictionary, exit: Dictionary) -> String:
	return "%d,%d,%d,%d" % [int(recover.mods), int(recover.vk), int(exit.mods), int(exit.vk)]


static func canonical(spec: String) -> String:
	var parsed := parse(spec)
	return str(parsed.canonical) if parsed.ok else ""


## Registers both bindings through the native bridge. Returns {ok, recover,
## exit, applied, message}; `ok` is true only when both hotkeys are live and
## `applied` is true when the native call was actually made.
static func apply(bridge, recover_spec: String, exit_spec: String) -> Dictionary:
	if bridge == null or not bridge.has_method("configure_hotkeys"):
		return {"ok": false, "recover": false, "exit": false, "applied": false,
			"message": "当前原生模块不支持自定义热键，仍使用 " + DEFAULT_RECOVER + " / " + DEFAULT_EXIT + "。"}
	var recover := parse(recover_spec)
	if not recover.ok:
		return {"ok": false, "recover": false, "exit": false, "applied": false,
			"message": "找回热键无效：" + str(recover.error)}
	var exit := parse(exit_spec)
	if not exit.ok:
		return {"ok": false, "recover": false, "exit": false, "applied": false,
			"message": "退出热键无效：" + str(exit.error)}
	if int(recover.mods) == int(exit.mods) and int(recover.vk) == int(exit.vk):
		return {"ok": false, "recover": false, "exit": false, "applied": false,
			"message": "找回和退出不能是同一个热键。"}
	var bits := int(bridge.configure_hotkeys(encode(recover, exit)))
	var recover_live := (bits & 1) != 0
	var exit_live := (bits & 2) != 0
	if recover_live and exit_live:
		return {"ok": true, "recover": true, "exit": true, "applied": true, "message": ""}
	var failed: Array[String] = []
	if not recover_live:
		failed.append(str(recover.canonical))
	if not exit_live:
		failed.append(str(exit.canonical))
	return {"ok": false, "recover": recover_live, "exit": exit_live, "applied": true,
		"message": " / ".join(failed) + " 已被其它程序占用，没能生效。"}


## Applies the wanted pair and, when native registration failed, puts the
## previously working pair back. Returns {ok, active, message}: `active` is what
## is really registered, so the caller can persist a corrected document instead
## of leaving the file and the process out of sync.
static func resolve(bridge, wanted_recover: String, wanted_exit: String,
		active: Array) -> Dictionary:
	var outcome := apply(bridge, wanted_recover, wanted_exit)
	if outcome.ok:
		outcome["active"] = [canonical(wanted_recover), canonical(wanted_exit)]
		return outcome
	var fallback: Array = []
	for index in range(active.size()):
		fallback.append(str(active[index]))
	if fallback.size() != 2:
		fallback = defaults()
		if bool(outcome.get("applied", false)):
			apply(bridge, fallback[0], fallback[1])
	elif bool(outcome.get("applied", false)):
		# A conflict already unregistered both bindings; restore the pair that
		# worked so the pet stays recoverable and closable.
		apply(bridge, fallback[0], fallback[1])
	outcome["active"] = fallback
	return outcome
