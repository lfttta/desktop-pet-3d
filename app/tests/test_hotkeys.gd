extends SceneTree
## Global hotkey text parsing, native registration fallback and the settings
## page controls. The native bridge is a fake; registration output is the only
## thing the module depends on.

const Hotkeys = preload("res://scripts/platform/hotkeys.gd")
const SettingsWindow = preload("res://scripts/ui/settings_window.gd")

var checks := 0
var failures := 0

func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	_test_parse()
	_test_apply()
	_test_resolve()
	await _test_settings_controls()
	print("HOTKEYS_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _test_parse() -> void:
	var recover := Hotkeys.parse("Ctrl+Alt+F10")
	check(recover.ok, "the default recover binding parses")
	check(int(recover.mods) == 0x0003, "Ctrl+Alt maps to MOD_CONTROL|MOD_ALT")
	check(int(recover.vk) == 0x79, "F10 maps to VK_F10")
	check(str(recover.canonical) == "Ctrl+Alt+F10", "the default binding is already canonical")

	var exit := Hotkeys.parse(" control + option + q ")
	check(exit.ok, "modifier aliases and surrounding spaces are accepted")
	check(str(exit.canonical) == "Ctrl+Alt+Q", "aliases canonicalise to Ctrl+Alt+Q")
	check(int(exit.vk) == 0x51, "q maps to its uppercase virtual key")

	var ordered := Hotkeys.parse("shift+alt+ctrl+f24")
	check(ordered.ok, "three modifiers are accepted")
	check(str(ordered.canonical) == "Ctrl+Alt+Shift+F24", "modifier order is normalised")
	check(int(ordered.vk) == 0x87, "F24 maps to the last function key")

	check(str(Hotkeys.parse("Ctrl+9").canonical) == "Ctrl+9", "digits are valid keys")
	check(str(Hotkeys.parse("Ctrl+PageDown").canonical) == "Ctrl+PageDown",
		"page keys use their canonical names")
	check(str(Hotkeys.parse("Ctrl+pgdn").canonical) == "Ctrl+PageDown",
		"page key aliases canonicalise")
	check(str(Hotkeys.parse("Ctrl+esc").canonical) == "Ctrl+Esc", "Escape aliases to Esc")

	for bad in ["", "F10", "Ctrl", "Ctrl+Alt", "Ctrl+Alt+", "+Q", "Ctrl+Alt+Q+F1",
			"Ctrl+Alt+F25", "Ctrl+Alt+中", "Ctrl+Ctrl+Q"]:
		var parsed := Hotkeys.parse(str(bad))
		check(not parsed.ok and not str(parsed.error).is_empty(),
			"rejects the unsupported binding %s" % [bad])

	check(Hotkeys.defaults() == ["Ctrl+Alt+F10", "Ctrl+Alt+Q"],
		"defaults mirror the native initial bindings")
	check(Hotkeys.bindings({}) == Hotkeys.defaults(),
		"a settings document without the group falls back to the defaults")
	check(Hotkeys.bindings({"hotkeys": {"recover": "Alt+F9"}}) == ["Alt+F9", "Ctrl+Alt+Q"],
		"a partial group falls back per binding")

	check(Hotkeys.canonical("ctrl+alt+f10") == "Ctrl+Alt+F10",
		"canonical() returns the stored spelling")
	check(Hotkeys.canonical("F10") == "", "canonical() is empty for invalid text")


func _test_apply() -> void:
	var bridge := FakeBridge.new()
	var ok := Hotkeys.apply(bridge, "Ctrl+Alt+F10", "Ctrl+Alt+Q")
	check(ok.ok and ok.recover and ok.exit, "registering both bindings reports success")
	check(ok.applied, "apply marks that the native call happened")
	check(bridge.calls == ["3,121,3,81"],
		"the native spec carries mods,vk,mods,vk")

	bridge = FakeBridge.new()
	bridge.bits = 1
	var partial := Hotkeys.apply(bridge, "Ctrl+Alt+F10", "Ctrl+Alt+Q")
	check(not partial.ok and partial.recover and not partial.exit,
		"a half-registered pair reports which side failed")
	check(str(partial.message).contains("Ctrl+Alt+Q"),
		"the conflict message names the binding that failed")

	bridge = FakeBridge.new()
	var invalid := Hotkeys.apply(bridge, "F10", "Ctrl+Alt+Q")
	check(not invalid.ok and not invalid.applied, "invalid text never reaches the native call")
	check(bridge.calls.is_empty(), "invalid text leaves the registered pair untouched")

	bridge = FakeBridge.new()
	var duplicate := Hotkeys.apply(bridge, "Ctrl+Alt+Q", "alt+ctrl+q")
	check(not duplicate.ok and not duplicate.applied, "identical bindings are refused")
	check(bridge.calls.is_empty(), "identical bindings never reach the native call")

	var missing := Hotkeys.apply(null, "Ctrl+Alt+F10", "Ctrl+Alt+Q")
	check(not missing.ok and not missing.applied, "a missing bridge is reported, not crashed")


func _test_resolve() -> void:
	var bridge := FakeBridge.new()
	var applied := Hotkeys.resolve(bridge, "Ctrl+Alt+F10", "Ctrl+Alt+Q", [])
	check(applied.ok, "resolve reports success for a working pair")
	check(applied.active == ["Ctrl+Alt+F10", "Ctrl+Alt+Q"],
		"resolve reports the canonical active pair")

	bridge = FakeBridge.new()
	bridge.bits = 2
	var fallback := Hotkeys.resolve(bridge, "Ctrl+Alt+F10", "Ctrl+Shift+X",
		["Ctrl+Alt+F10", "Ctrl+Alt+Q"])
	check(not fallback.ok, "resolve reports the failed registration")
	check(fallback.active == ["Ctrl+Alt+F10", "Ctrl+Alt+Q"],
		"resolve keeps the pair that worked as the active pair")
	check(bridge.calls.size() == 2 and bridge.calls[1] == "3,121,3,81",
		"resolve re-registers the working pair after a conflict")

	bridge = FakeBridge.new()
	var untouched := Hotkeys.resolve(bridge, "bad", "Ctrl+Alt+Q", ["Ctrl+Alt+F10", "Ctrl+Alt+Q"])
	check(not untouched.ok and not untouched.applied, "resolve reports invalid text")
	check(bridge.calls.is_empty(), "invalid text does not disturb the active pair")


func _test_settings_controls() -> void:
	var host := Node.new()
	root.add_child(host)
	var fake := FakeService.new()
	fake.settings = {"schema_version": 8}
	host.add_child(fake)
	var window := SettingsWindow.new()
	window.visible = false
	host.add_child(window)
	await process_frame
	window.show_status("ready", "后台服务已连接")

	var settings := _settings(8)
	window.populate(settings, [])
	await process_frame
	check(window.hotkey_recover != null and window.hotkey_exit != null,
		"the basic page has both hotkey fields")
	check(window.hotkey_recover.text == "Control+Option+F10",
		"populate keeps the stored spelling")
	check(window.hotkey_exit.text == "Ctrl+Shift+X", "populate restores the exit binding")
	check(window.hotkey_recover.editable and window.hotkey_exit.editable,
		"a v8 service unlocks the hotkey fields")

	var patches: Array = []
	window.save_requested.connect(func(value: Dictionary): patches.append(value))
	window._save()
	check(patches.size() == 1, "a v8 save emits one patch")
	var patch: Dictionary = patches[0]
	check(patch.has("hotkeys"), "the save patch carries the hotkeys group")
	check(str(patch.hotkeys.recover) == "Ctrl+Alt+F10",
		"the recover binding is canonicalised on save")
	check(str(patch.hotkeys.exit) == "Ctrl+Shift+X",
		"modifier order is canonicalised on save")
	check(window.hotkey_recover.text == "Ctrl+Alt+F10",
		"the field shows the canonical spelling after save")

	# Invalid text must not be sent at all.
	window.hotkey_recover.text = "F10"
	patches.clear()
	window._save()
	check(patches.is_empty(), "an invalid hotkey blocks the whole save")
	check(not window.message.text.is_empty(), "an invalid hotkey explains itself")

	window.hotkey_recover.text = "Ctrl+Alt+Q"
	window.hotkey_exit.text = "Alt+Ctrl+Q"
	patches.clear()
	window._save()
	check(patches.is_empty(), "identical bindings block the whole save")

	# Defaults fill a document without the group, and an older service neither
	# receives nor unlocks the group.
	window.hotkey_recover.text = "Ctrl+Alt+F10"
	window.hotkey_exit.text = "Ctrl+Alt+Q"
	window.populate({"schema_version": 8}, [])
	await process_frame
	check(window.hotkey_recover.text == "Ctrl+Alt+F10"
		and window.hotkey_exit.text == "Ctrl+Alt+Q",
		"a document without the group shows the defaults")

	fake.settings = {"schema_version": 7}
	window.show_status("ready", "后台服务已连接")
	window.populate(_settings(7), [])
	await process_frame
	check(not window.hotkey_recover.editable and not window.hotkey_exit.editable,
		"a pre-v8 service locks the hotkey fields")
	patches.clear()
	window._save()
	check(patches.size() == 1 and not patches[0].has("hotkeys"),
		"a pre-v8 patch leaves the hotkeys group out")

	window.show_hotkeys(["Ctrl+Alt+F7", "Ctrl+Alt+F8"])
	check(window.hotkey_recover.text == "Ctrl+Alt+F7"
		and window.hotkey_exit.text == "Ctrl+Alt+F8",
		"show_hotkeys puts the registered pair back into the fields")
	window.show_hotkeys([])
	check(window.hotkey_recover.text == "Ctrl+Alt+F7",
		"show_hotkeys ignores an empty active pair")

	window.free()
	host.free()


func _settings(version: int) -> Dictionary:
	var document := {"schema_version": version}
	if version >= 8:
		document["hotkeys"] = {
			"recover": "Control+Option+F10",
			"exit": "Ctrl+Shift+X",
		}
	return document


class FakeBridge extends RefCounted:
	var calls: Array = []
	var bits := 3

	func configure_hotkeys(spec: String) -> int:
		calls.append(spec)
		return bits


class FakeService extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	var state := "ready"
	var features := {}
	var personas: Array = []
	var settings := {}
	var _sequence := 0

	func request(type: String, payload: Dictionary, timeout := 8.0) -> String:
		_sequence += 1
		return "fake-%d" % _sequence
