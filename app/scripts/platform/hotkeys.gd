extends RefCounted
## Global hotkey text parsing and registration.
##
## The settings file stores bindings as "Ctrl+Alt+F10" style text so they stay
## readable and editable. Native `configure_hotkeys` takes Win32 modifier bits
## and virtual-key codes in one "mods,vk,mods,vk,mods,vk" string (recover, exit,
## chat), which this module builds. Registration is process-wide, so a failure
## means another program already owns the combination; that outcome is reported,
## never hidden.

const MOD_ALT := 0x0001
const MOD_CONTROL := 0x0002
const MOD_SHIFT := 0x0004
const MOD_WIN := 0x0008

const DEFAULT_RECOVER := "Ctrl+Alt+F10"
const DEFAULT_EXIT := "Ctrl+Alt+Q"
const DEFAULT_CHAT := "Ctrl+Alt+C"

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
	return [DEFAULT_RECOVER, DEFAULT_EXIT, DEFAULT_CHAT]


## Reads the triple out of a settings document, falling back per binding.
static func bindings(settings: Dictionary) -> Array:
	var group: Variant = settings.get("hotkeys", {})
	if not group is Dictionary:
		group = {}
	return [str(group.get("recover", DEFAULT_RECOVER)), str(group.get("exit", DEFAULT_EXIT)),
		str(group.get("chat", DEFAULT_CHAT))]


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


static func encode(recover: Dictionary, exit: Dictionary, chat: Dictionary) -> String:
	return "%d,%d,%d,%d,%d,%d" % [int(recover.mods), int(recover.vk), int(exit.mods),
		int(exit.vk), int(chat.mods), int(chat.vk)]


static func canonical(spec: String) -> String:
	var parsed := parse(spec)
	return str(parsed.canonical) if parsed.ok else ""


## Registers all three bindings through the native bridge. Returns
## {ok, recover, exit, chat, applied, message}; `ok` is true only when all three
## hotkeys are live and `applied` is true when the native call was actually made.
static func apply(bridge, recover_spec: String, exit_spec: String,
		chat_spec: String = DEFAULT_CHAT) -> Dictionary:
	var failed_all := {"ok": false, "recover": false, "exit": false, "chat": false,
		"applied": false}
	if bridge == null or not bridge.has_method("configure_hotkeys"):
		failed_all["message"] = ("当前原生模块不支持自定义热键，仍使用 " + DEFAULT_RECOVER
			+ " / " + DEFAULT_EXIT + " / " + DEFAULT_CHAT + "。")
		return failed_all
	var recover := parse(recover_spec)
	if not recover.ok:
		failed_all["message"] = "找回热键无效：" + str(recover.error)
		return failed_all
	var exit := parse(exit_spec)
	if not exit.ok:
		failed_all["message"] = "退出热键无效：" + str(exit.error)
		return failed_all
	var chat := parse(chat_spec)
	if not chat.ok:
		failed_all["message"] = "聊天热键无效：" + str(chat.error)
		return failed_all
	var parsed := {"recover": recover, "exit": exit, "chat": chat}
	var labels := {"recover": "找回", "exit": "退出", "chat": "聊天"}
	var names := ["recover", "exit", "chat"]
	for first_index in range(names.size()):
		for second_index in range(first_index + 1, names.size()):
			var first: Dictionary = parsed[names[first_index]]
			var second: Dictionary = parsed[names[second_index]]
			if int(first.mods) == int(second.mods) and int(first.vk) == int(second.vk):
				failed_all["message"] = "%s和%s不能是同一个热键。" % [
					str(labels[names[first_index]]), str(labels[names[second_index]])]
				return failed_all
	var bits := int(bridge.configure_hotkeys(encode(recover, exit, chat)))
	var recover_live := (bits & 1) != 0
	var exit_live := (bits & 2) != 0
	var chat_live := (bits & 4) != 0
	if recover_live and exit_live and chat_live:
		return {"ok": true, "recover": true, "exit": true, "chat": true,
			"applied": true, "message": ""}
	var failed: Array[String] = []
	if not recover_live:
		failed.append(str(recover.canonical))
	if not exit_live:
		failed.append(str(exit.canonical))
	if not chat_live:
		failed.append(str(chat.canonical))
	return {"ok": false, "recover": recover_live, "exit": exit_live, "chat": chat_live,
		"applied": true, "message": " / ".join(failed) + " 已被其它程序占用，没能生效。"}


## Applies the wanted triple and, when native registration failed, puts the
## previously working set back. Returns {ok, active, message}: `active` is what
## is really registered, so the caller can persist a corrected document instead
## of leaving the file and the process out of sync. `active` is empty when even
## the fallback could not be registered; the caller must then avoid writing any
## hotkey back. `wanted_chat` is optional so two-binding callers keep working;
## without it chat stays at the native default and is not reported in `active`.
static func resolve(bridge, wanted_recover: String, wanted_exit: String,
		active: Array, wanted_chat: String = "") -> Dictionary:
	var managed := not wanted_chat.is_empty()
	var chat := wanted_chat if managed else DEFAULT_CHAT
	var outcome := apply(bridge, wanted_recover, wanted_exit, chat)
	var wanted := [canonical(wanted_recover), canonical(wanted_exit), canonical(chat)]
	var wanted_size := 3 if managed else 2
	if outcome.ok:
		outcome["active"] = wanted.slice(0, wanted_size)
		return outcome
	var fallback: Array = []
	for index in range(active.size()):
		fallback.append(str(active[index]))
	if managed:
		if fallback.size() != 3:
			fallback = defaults()
	else:
		# A legacy caller only knows recover/exit; keep that pair so it can be
		# persisted back when a conflict forced the defaults.
		if fallback.size() < 2:
			fallback = defaults().slice(0, 2)
		else:
			fallback = fallback.slice(0, 2)
	if bool(outcome.get("applied", false)):
		# A conflict already unregistered the bindings; restore the set that
		# worked so the pet stays recoverable and closable. A two-binding caller
		# cannot vouch for chat, so it returns to the native default.
		var restore_chat := str(fallback[2]) if fallback.size() > 2 else chat
		var restored := apply(bridge, str(fallback[0]), str(fallback[1]), restore_chat)
		if not restored.ok:
			# Even the defaults are taken. Claiming them as active would make the
			# caller persist a triple that is not registered, which is exactly the
			# "settings says it works but the key does nothing" failure.
			outcome["active"] = []
			outcome["message"] = "当前没有可用的全局热键；请在设置里换一组组合。"
			return outcome
	outcome["active"] = fallback
	return outcome
