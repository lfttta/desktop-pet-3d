extends SceneTree
## Greeting rules: the wall-clock window, the click cooldown and the service
## switches. The phrase library is the shipping one; the clock is injected so
## the checks never depend on when the suite runs and never wait for a cooldown.

const PetWindow = preload("res://scripts/pet_window.gd")
const MainWindow = preload("res://scripts/main.gd")
const PHRASES := "res://../data/expression/phrases.json"
const CLICK_CATEGORIES := ["greeting", "care"]
const GREETING_CATEGORIES := ["greeting"]

var checks := 0
var failures := 0


## Wall clock the window reads through `greeting_clock`. The test owns the hour
## and the minute count, so "advance past the cooldown" is one assignment.
class FixedClock extends RefCounted:
	var hour := 0
	var minutes := 0.0

	func now() -> Dictionary:
		return {"hour": hour, "minutes": minutes}


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("_run")


func _run() -> void:
	_test_time_windows()
	_test_cooldown()
	_test_settings_handoff()
	await _test_displays()
	print("GREETING_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)


## The window boundaries must match service/expression/phrases.py: half-open
## ranges, with night wrapping midnight.
func _test_time_windows() -> void:
	for pair in [[5, "morning"], [10, "morning"], [11, "afternoon"], [16, "afternoon"],
			[17, "evening"], [21, "evening"], [22, "night"], [4, "night"], [0, "night"]]:
		var hour: int = pair[0]
		check(PetWindow.time_window_for(hour) == pair[1],
			"hour %d belongs to %s, got %s" % [hour, pair[1], PetWindow.time_window_for(hour)])

	# Drawing many times per hour covers the random choice: every result must
	# belong to that hour's window (or to `any`), never to another window.
	var window := PetWindow.new()
	var clock := FixedClock.new()
	window.greeting_clock = clock.now
	for hour in [0, 4, 5, 6, 10, 11, 12, 16, 17, 18, 21, 22, 23]:
		var name := PetWindow.time_window_for(hour)
		var allowed := _texts_for(hour, name, CLICK_CATEGORIES)
		check(not allowed.is_empty(), "the library has click phrases for hour %d" % hour)
		clock.hour = hour
		for _index in 40:
			# Two hours between draws clears the default 60 minute cooldown.
			clock.minutes += 120.0
			var phrase := window._pick_click_phrase()
			check(not phrase.is_empty(), "hour %d yields a click phrase" % hour)
			check(allowed.has(phrase), "hour %d returned a phrase outside its window: '%s'" % [hour, phrase])
	window.free()


## A second click inside greeting.cooldown_minutes does not replace the words;
## once the cooldown has passed the click may pick a new phrase.
func _test_cooldown() -> void:
	var window := PetWindow.new()
	var clock := FixedClock.new()
	clock.hour = 8
	clock.minutes = 1000.0
	window.greeting_clock = clock.now
	window.set_greeting_settings(true, true, 60)

	var first := window._pick_click_phrase()
	check(not first.is_empty(), "the first click says something")

	clock.minutes += 30.0
	check(window._pick_click_phrase().is_empty(),
		"a click 30 minutes later stays silent inside the cooldown")

	clock.minutes += 29.0
	check(window._pick_click_phrase().is_empty(),
		"a click 59 minutes later is still inside the cooldown")

	clock.minutes += 2.0
	check(not window._pick_click_phrase().is_empty(),
		"a click 61 minutes later may pick new words")
	window.free()


## main.gd reads the service's greeting group and hands it to the window; a
## missing group or an out-of-range cooldown must not reset what was applied.
func _test_settings_handoff() -> void:
	var window := MainWindow.new()
	window.saved_settings = {"bubble": {"enabled": true, "duration_seconds": 7, "show_replies": false}}
	window._apply_bubble_settings()
	# Click lines and the start greeting pass this value explicitly; before that
	# they fell back to the per-text default while the setting only covered the
	# bubbles main.gd shows.
	check(is_equal_approx(window._bubble_duration, 7.0), "main passes bubble.duration_seconds to the window")

	window.saved_settings = {"greeting": {"enabled": false, "on_start": false, "cooldown_minutes": 15}}
	window._apply_greeting_settings()
	check(not bool(window.greeting.get("enabled", true)), "main reads greeting.enabled")
	check(not bool(window.greeting.get("on_start", true)), "main reads greeting.on_start")
	check(int(window.greeting.get("cooldown_minutes", 0)) == 15, "main reads greeting.cooldown_minutes")

	window.saved_settings = {"greeting": {"cooldown_minutes": 2}}
	window._apply_greeting_settings()
	check(int(window.greeting.get("cooldown_minutes", 0)) == 15,
		"an out-of-range cooldown keeps the previous value")

	window.saved_settings = {}
	window._apply_greeting_settings()
	check(not bool(window.greeting.get("enabled", true)),
		"a pre-v7 document leaves the applied greeting switches alone")
	window.free()


## The switches gate the display itself, not just the phrase choice.
func _test_displays() -> void:
	var window := PetWindow.new()
	root.add_child(window)
	await process_frame
	var clock := FixedClock.new()
	clock.hour = 8
	clock.minutes = 5000.0
	window.greeting_clock = clock.now

	# greeting.enabled = false silences both the start greeting and the click.
	window.set_greeting_settings(false, true, 60)
	window.greet_on_start()
	check(not _bubble_visible(window), "greeting.enabled=false keeps the start greeting silent")
	check(window._pick_click_phrase().is_empty(), "greeting.enabled=false keeps a click silent")

	# greeting.on_start = false silences the start line only; clicks still speak.
	window.set_greeting_settings(true, false, 60)
	window.greet_on_start()
	check(not _bubble_visible(window), "greeting.on_start=false keeps the start greeting silent")
	window._say_click_phrase()
	check(_bubble_visible(window), "with on_start off a click still shows its phrase")
	var click_text: String = window.speech_bubble.label.text
	check(_texts_for(clock.hour, PetWindow.time_window_for(clock.hour), CLICK_CATEGORIES).has(click_text),
		"the click phrase matches the hour: '%s'" % click_text)
	window.hide_bubble()

	# With both switches on the start greeting appears, and it is a greeting line
	# rather than a care reminder.
	clock.minutes += 120.0
	window.set_greeting_settings(true, true, 60)
	window.greet_on_start()
	check(_bubble_visible(window), "greeting.on_start=true shows the start greeting")
	var start_text: String = window.speech_bubble.label.text
	check(_texts_for(clock.hour, PetWindow.time_window_for(clock.hour), GREETING_CATEGORIES).has(start_text),
		"the start greeting is a greeting for the hour: '%s'" % start_text)
	window.hide_bubble()
	window.free()


func _bubble_visible(window) -> bool:
	return is_instance_valid(window.speech_bubble) and window.speech_bubble.visible


## Enabled phrases of `categories` valid at `hour`, read from the library the
## window itself reads. An unknown or missing window means every hour.
func _texts_for(hour: int, window: String, categories: Array) -> Array[String]:
	var texts: Array[String] = []
	for entry in _library():
		if not categories.has(str(entry.get("category", ""))):
			continue
		if not bool(entry.get("enabled", true)):
			continue
		var entry_window := str(entry.get("time_window", "any"))
		if entry_window != "any" and entry_window != window:
			continue
		var text := str(entry.get("text", "")).strip_edges()
		if not text.is_empty():
			texts.append(text)
	return texts


func _library() -> Array:
	var path := ProjectSettings.globalize_path(PHRASES)
	if not FileAccess.file_exists(path):
		push_error("phrase library missing at " + path)
		return []
	var data: Variant = JSON.parse_string(FileAccess.get_file_as_string(path))
	if not (data is Dictionary):
		return []
	var entries: Variant = (data as Dictionary).get("phrases", [])
	if entries is Array:
		return entries
	return []
