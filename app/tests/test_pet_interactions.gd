extends SceneTree
## Desktop interaction rules added on top of the stage-0 window:
## wheel zoom + delayed save, double-click-to-chat, hover/region reactions,
## menu walk, loop-action timeout, hidden fps while utility windows are open,
## timer bubbles and utility window fitting.
##
## Everything runs headless. The real `PetWindow` is used for the animation and
## input paths; `main.gd` is instantiated without entering the tree so its
## `_ready` never starts the Python service.

const PetWindow = preload("res://scripts/pet_window.gd")
const MainWindow = preload("res://scripts/main.gd")
const HitRegions = preload("res://scripts/hit_regions.gd")
const AppSettings = preload("res://scripts/app_settings.gd")

const WORK_AREA := Rect2i(0, 0, 1920, 1000)

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
	await _test_hit_region_ids()
	await _test_hover_and_click_parts()
	await _test_double_click()
	await _test_menu_walk()
	await _test_reselect_walk()
	await _test_loop_action_timeout()
	_test_wheel_zoom()
	_test_hidden_fps()
	_test_primary_screen_only()
	_test_utility_window_fit()
	_test_timer_event()
	_test_timer_event_deferred_while_hidden()
	_test_server_event_forwarding()
	print("PET_INTERACTION_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)


## Character regions carry the ids the reaction code keys on.
func _test_hit_region_ids() -> void:
	var synthetic := [
		{"id": "head", "a": Vector2(0, 0), "b": Vector2(0, 0), "radius": 5.0},
		{"id": "hand", "a": Vector2(20, 0), "b": Vector2(20, 0), "radius": 5.0},
	]
	check(HitRegions.id_at(Vector2(1, 1), synthetic) == "head", "id_at reports the head region")
	check(HitRegions.id_at(Vector2(22, 1), synthetic) == "hand", "id_at reports the hand region")
	check(HitRegions.id_at(Vector2(100, 100), synthetic) == "", "id_at reports no region outside the body")
	check(HitRegions.contains(Vector2(1, 1), synthetic), "contains still works through id_at")

	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	var ids := {}
	if pet.actor and pet.camera and pet.actor.has_method("project_hit_regions"):
		for region: Dictionary in pet.actor.project_hit_regions(pet.camera):
			ids[str(region.get("id", ""))] = true
	check(ids.has("head"), "the character projects a head region")
	check(ids.has("hand"), "the character projects hand regions")
	check(ids.has("body"), "the character projects a body region")
	pet.free()


## Hover must wait 0.6 s, fire once, and restore on leave. A head click adds
## happy; a hand click only waves.
func _test_hover_and_click_parts() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	var view = pet.actor
	if view == null:
		check(false, "the shipping window loaded a character")
		pet.free()
		return
	view.set_emotion("neutral", 1.0)
	pet._advance_hover("head", 0.59)
	check(view.emotion == "neutral", "0.59 s of hover does not change the face")
	pet._advance_hover("head", 0.02)
	check(view.emotion == "happy", "0.6 s of head hover shows happy")
	check(pet._hover_happy, "the hover trigger latches instead of re-firing")
	pet._advance_hover("head", 0.5)
	check(view.emotion == "happy" and pet._hover_happy,
		"staying on the head does not retrigger or reset the face")
	pet._advance_hover("", 0.01)
	check(view.emotion == "neutral", "leaving the head restores the face")
	check(not pet._hover_happy, "leaving clears the hover latch")

	# The timer restarts after a leave, so a quick re-entry does not inherit the
	# previous 0.6 s.
	pet._advance_hover("head", 0.3)
	pet._advance_hover("", 0.01)
	pet._advance_hover("head", 0.3)
	check(view.emotion == "neutral", "a re-entry restarts the 0.6 s timer")

	pet._play_action("idle")
	check(pet._handle_body_click(Vector2i(10, 10), "hand", 1000) == "wave",
		"a hand click is a wave")
	check(view.action == "wave", "the hand click plays the wave clip")
	check(view.emotion == "neutral", "a hand click does not change the face")

	pet._play_action("idle")
	check(pet._handle_body_click(Vector2i(10, 10), "head", 2000) == "wave",
		"a head click is still a wave")
	check(view.emotion == "happy", "a head click adds the happy face")
	pet._update_happy(1.0)
	check(view.emotion == "happy", "the head click face lasts past one second")
	pet._update_happy(0.6)
	check(view.emotion == "neutral", "the head click face expires on its own")
	pet.free()


## Two clicks inside 500 ms (the Windows default double-click time) and 6 px
## open chat; the first still waves. Outside either bound the second click is a
## new single click.
func _test_double_click() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	var chats := [0]
	pet.bubble_chat_requested.connect(func(): chats[0] += 1)

	pet._play_action("idle")
	check(pet._handle_body_click(Vector2i(100, 100), "body", 1000) == "wave",
		"the first click waves")
	check(pet._handle_body_click(Vector2i(103, 102), "body", 1200) == "chat",
		"a second click 3 px away after 200 ms opens chat")
	check(chats[0] == 1, "the double click asks for the chat window")
	check(pet._handle_body_click(Vector2i(103, 102), "body", 1250) == "wave",
		"the double click pair is consumed")

	# A system-paced double click (400-450 ms) must still open chat.
	check(pet._handle_body_click(Vector2i(100, 100), "body", 2000) == "wave"
		and pet._handle_body_click(Vector2i(101, 100), "body", 2440) == "chat",
		"a second click after 440 ms opens chat")
	check(chats[0] == 2, "the system-paced double click asks for the chat window")

	pet._play_action("idle")
	check(pet._handle_body_click(Vector2i(100, 100), "body", 3000) == "wave"
		and pet._handle_body_click(Vector2i(110, 100), "body", 3100) == "wave",
		"a second click 10 px away is not a double click")
	check(chats[0] == 2, "the far click does not open chat")

	check(pet._handle_body_click(Vector2i(100, 100), "body", 4000) == "wave"
		and pet._handle_body_click(Vector2i(100, 100), "body", 4600) == "wave",
		"a second click after 600 ms is not a double click")
	check(chats[0] == 2, "the slow click does not open chat")
	pet.free()


## A menu walk is a real travelling segment from idle on the ground line; from
## any other state the clip falls back to playing in place. Near a wall the plan
## must shrink to the cycles that actually fit.
func _test_menu_walk() -> void:
	# A window restored before the motion controller learned the desktop used to
	# pass the ground-line guard (0 == 0) and then snap the soles to the bottom
	# edge, dropping the pet a full body height. The guard now runs after a bounds
	# sync, so the walk is refused instead.
	var unsynced := PetWindow.new()
	root.add_child(unsynced)
	await process_frame
	await process_frame
	unsynced.behavior_enabled = false
	unsynced.work_area_override = WORK_AREA
	unsynced.motion.place_at(Vector2(960.0, 0.0))
	check(not unsynced._begin_menu_walk(),
		"an unsynced motion controller refuses a menu walk")
	check(not unsynced._walk_active, "the refused walk leaves no active segment")
	check(unsynced.motion.anchor.y < float(WORK_AREA.end.y) - 1.0,
		"the refused walk does not snap the soles to the ground, y=%f"
		% unsynced.motion.anchor.y)
	unsynced.free()

	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	pet.work_area_override = WORK_AREA
	pet._ensure_motion_bounds()
	pet.motion.settle_at(960.0)
	pet._motion_active = false
	pet._play_action("idle")

	pet._play_action("walk")
	check(pet._walk_active, "a menu walk starts a travelling segment")
	check(pet.current_action == "walk", "the walk clip is playing")
	check(pet.motion.walk_cycles_total() == 2,
		"a roomy menu walk plans the preferred two cycles, got %d" % pet.motion.walk_cycles_total())
	pet._finish_walk(false)
	pet._motion_active = false
	pet._play_action("idle")

	# Near a wall the plan must not keep the two-cycle preference: with room for
	# only one cycle, walking two would clamp the body against the edge and slide
	# it in place for the last stretch.
	var subject: Rect2 = pet._subject_rect()
	var stride: float = pet._walk_stride_px()
	check(stride > 0.0, "the wall-limited case can measure the stride")
	var narrow := int(ceil(subject.size.x + stride * 2.2))
	pet.work_area_override = Rect2i(0, 0, narrow, WORK_AREA.size.y)
	pet._ensure_motion_bounds()
	pet.motion.settle_at(float(narrow) * 0.5)
	pet._motion_active = false
	pet._play_action("idle")
	pet._play_action("walk")
	check(pet._walk_active, "a wall-limited menu walk still starts")
	check(pet.motion.walk_cycles_total() == 1,
		"the plan shrinks to the single cycle that fits, got %d" % pet.motion.walk_cycles_total())
	check(pet.motion.anchor.x + stride <= float(narrow) - subject.size.x * 0.5 + 0.5,
		"the plan target stays inside the work area")
	pet._finish_walk(false)
	pet._motion_active = false
	pet._play_action("idle")
	pet.work_area_override = WORK_AREA
	pet._ensure_motion_bounds()
	pet.motion.settle_at(900.0)
	pet._motion_active = false

	# A pet left above the ground line cannot start a walk; the menu must still
	# answer with the in-place clip rather than doing nothing.
	pet.motion.place_at(Vector2(900, 600))
	pet._play_action("walk")
	check(not pet._walk_active, "an airborne/off-ground pet does not start a walk")
	check(pet.current_action == "walk", "the menu walk falls back to the clip in place")
	pet._finish_walk(false)
	pet.motion.settle_at(900.0)
	pet._motion_active = false
	pet._play_action("idle")

	# A user action owns the body: walk falls back instead of cutting it off.
	pet._play_action("wave")
	pet._play_action("walk")
	check(not pet._walk_active, "walk does not replace a non-idle action")
	check(pet.current_action == "walk", "the fallback clip still plays")
	pet.free()


## Re-selecting Walk while the segment is running must not restart the clip.
## The restart seeks playback to 0, and the wrap detector would read that jump
## as a completed gait cycle and drag the anchor forward by up to a stride.
func _test_reselect_walk() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	pet.work_area_override = WORK_AREA
	pet._ensure_motion_bounds()
	pet.motion.settle_at(960.0)
	pet._motion_active = false
	pet._play_action("idle")
	pet._play_action("walk")
	check(pet._walk_active, "the walk starts for the re-select case")

	var view = pet.actor
	view.player.seek(view.walk_cycle_length() * 0.7)
	pet._update_walk()
	var before: float = pet.motion.anchor.x
	var cycles_before: int = pet.motion.walk_cycles_total()
	pet._play_action("walk")
	check(pet._walk_active, "re-selecting walk keeps the segment running")
	check(pet.motion.walk_cycles_total() == cycles_before,
		"re-selecting walk keeps the same plan")
	check(is_equal_approx(pet.motion.anchor.x, before),
		"re-selecting walk does not move the body, was %f now %f" % [before, pet.motion.anchor.x])
	# The false cycle used to appear right after the restart, on the next update.
	pet._update_walk()
	check(absf(pet.motion.anchor.x - before) < pet._walk_stride_px() * 0.6,
		"the clip restart is not read as a completed cycle, moved %f"
		% absf(pet.motion.anchor.x - before))
	pet._finish_walk(false)
	pet.free()


## Loop clips never complete, so a menu-selected one must return to idle after
## LOOP_ACTION_SECONDS. One-shot actions must not be touched by that timer, and
## neither must a walk: the plan owns its own end.
func _test_loop_action_timeout() -> void:
	var pet := PetWindow.new()
	root.add_child(pet)
	await process_frame
	await process_frame
	pet.behavior_enabled = false
	pet._play_action("sleeping_idle")
	check(pet.current_action == "sleeping_idle", "the loop clip starts")
	check(is_equal_approx(pet._loop_action_remaining, PetWindow.LOOP_ACTION_SECONDS),
		"a loop action arms the 8 s timeout")
	pet._update_loop_action(7.9)
	check(pet.current_action == "sleeping_idle", "the loop keeps playing before 8 s")
	pet._update_loop_action(0.2)
	check(pet.current_action == "idle", "the loop returns to idle after 8 s")

	pet._play_action("wave")
	check(pet._loop_action_remaining == 0.0, "a one-shot action arms no timeout")
	pet._update_loop_action(20.0)
	check(pet.current_action == "wave", "the timeout never cuts a one-shot action")

	# The in-place fallback walk still latches without a stop, so it must keep the
	# generic loop timeout; only the travelling segment is exempt.
	pet._play_action("walk")
	check(not pet._walk_active, "the fallback walk plays in place here")
	check(is_equal_approx(pet._loop_action_remaining, PetWindow.LOOP_ACTION_SECONDS),
		"an in-place walk clip still arms the loop timeout")
	pet._update_loop_action(PetWindow.LOOP_ACTION_SECONDS + 0.1)
	check(pet.current_action == "idle", "the in-place walk returns to idle after 8 s")

	# A walk is a travelling segment with a plan-defined end; an 8 s timeout only
	# long enough for eight cycles would cut it off and skip the position save.
	pet.work_area_override = WORK_AREA
	pet._ensure_motion_bounds()
	pet.motion.settle_at(960.0)
	pet._motion_active = false
	pet._play_action("idle")
	pet._play_action("walk")
	check(pet._walk_active, "a walk starts for the timeout case")
	check(pet._loop_action_remaining == 0.0, "a walk arms no loop timeout")
	pet._update_loop_action(20.0)
	check(pet._walk_active and pet.current_action == "walk",
		"the loop timeout never interrupts a walk")
	pet._finish_walk(false)
	pet.free()


func _test_wheel_zoom() -> void:
	var main := MainWindow.new()
	main.saved_settings = {"scale": 1.0}
	check(is_equal_approx(main._apply_wheel_delta(120), 1.05), "one notch up adds 0.05")
	check(is_equal_approx(float(main.saved_settings.scale), 1.05), "the new scale is stored")
	check(is_equal_approx(main._apply_wheel_delta(-240), 0.95), "two notches down subtract 0.1")
	check(main._wheel_save_due > 0, "the wheel arms the delayed save")

	main.saved_settings["scale"] = 2.0
	check(is_equal_approx(main._apply_wheel_delta(120), 2.0), "the scale is clamped at 2.0")
	main.saved_settings["scale"] = 0.5
	check(is_equal_approx(main._apply_wheel_delta(-120), 0.5), "the scale is clamped at 0.5")

	# One poll per frame drains the native accumulator exactly once.
	var wheel := FakeWheelBridge.new()
	wheel.deltas = [120, 0]
	main.bridge = wheel
	main.saved_settings["scale"] = 1.0
	main._poll_wheel()
	check(is_equal_approx(float(main.saved_settings.scale), 1.05), "polling applies the wheel notches")
	main._poll_wheel()
	check(is_equal_approx(float(main.saved_settings.scale), 1.05), "an empty poll changes nothing")

	# The save is delayed until 300 ms have passed, then carries only the scale.
	var service := FakeServiceClient.new()
	main.client = service
	main._apply_wheel_delta(120)
	main._flush_wheel_save(Time.get_ticks_msec())
	check(service.patches.is_empty(), "the scale is not saved before 300 ms")
	main._flush_wheel_save(Time.get_ticks_msec() + 400)
	check(service.patches.size() == 1, "the scale is saved after the quiet period")
	check(is_equal_approx(float(service.patches[0].scale), float(main.saved_settings.scale)),
		"the save patch carries the wheel-selected scale")
	check(main._wheel_save_due == 0, "the delayed save clears its deadline")
	main.free()


## Hidden fps must not drop while a utility window is visible, and must return
## to hidden_fps when that window closes.
func _test_hidden_fps() -> void:
	var main := MainWindow.new()
	main.hidden = true
	main.idle_fps = 30
	main.hidden_fps = 5
	check(main._hidden_fps_target() == 5, "hidden alone uses hidden_fps")
	main.settings_window = Window.new()
	main.settings_window.visible = true
	check(main._hidden_fps_target() == 30,
		"a visible settings window keeps the hidden pet at idle_fps, got %d" % main._hidden_fps_target())
	main.settings_window.visible = false
	main.chat_window = Window.new()
	main.chat_window.visible = true
	check(main._hidden_fps_target() == 30,
		"a visible chat window keeps the hidden pet at idle_fps")
	main.chat_window.visible = false
	check(main._hidden_fps_target() == 5, "closing both windows returns to hidden_fps")
	main.idle_fps = 10
	main.hidden_fps = 15
	main.chat_window.visible = true
	check(main._hidden_fps_target() >= 10,
		"the utility floor is at least idle_fps even when hidden_fps is higher")
	main.settings_window.free()
	main.chat_window.free()
	main.free()


## The pet is pinned to the primary screen. The cross-screen selector and its
## runtime lookup are gone, while `screen_id` stays in the document shape so the
## settings schema does not have to change.
func _test_primary_screen_only() -> void:
	var main := MainWindow.new()
	check(not main.has_method("_select_display"), "main no longer selects a display")
	var settings := AppSettings.new()
	check(not settings.has_method("select_display"), "app settings has no display selector")
	check(AppSettings.defaults().has("screen_id"),
		"screen_id is kept in the defaults for schema compatibility")
	main.free()


## Utility windows are shrunk to the primary work area, then centered.
func _test_utility_window_fit() -> void:
	check(AppSettings.fit_window_size(Vector2i(720, 560), Rect2i(0, 0, 1024, 768), 16) ==
		Vector2i(720, 560), "a window that fits keeps its design size")
	check(AppSettings.fit_window_size(Vector2i(720, 560), Rect2i(0, 0, 640, 480), 16) ==
		Vector2i(608, 448), "a small work area shrinks the window by the margin")

	var main := MainWindow.new()
	var window := Window.new()
	window.min_size = Vector2i(720, 560)
	window.size = Vector2i(720, 560)
	window.set_meta("preferred_size", Vector2i(720, 560))
	var work := Rect2i(0, 0, 640, 480)
	main._place_utility_window(window, work)
	check(window.size == Vector2i(608, 448), "the utility window shrinks to the work area")
	check(window.position == Vector2i(16, 16), "the shrunk window is centered in the work area")
	check(work.encloses(Rect2i(window.position, window.size)),
		"the utility window stays inside the work area")

	var chat := Window.new()
	chat.size = Vector2i(520, 400)
	chat.set_meta("preferred_size", Vector2i(520, 400))
	main._place_utility_window(chat, Rect2i(0, 0, 400, 300))
	check(chat.size == Vector2i(368, 268), "the chat window shrinks on a smaller work area")
	window.free()
	chat.free()
	main.free()


## `timer.due` shows the reminder text with the configured bubble duration and
## ignores a repeated event for the same reminder. The dedupe key is id+due_at:
## the service reuses a cancelled timer's id, so the id alone would swallow the
## next reminder.
func _test_timer_event() -> void:
	var probe := TimerProbe.new()
	probe.bubble = {"enabled": true, "show_replies": true, "duration_seconds": 7}
	probe._on_service_event("timer", "timer.due",
		{"id": 11, "due_at": "2026-11-13T12:05:00+00:00", "text": " 喝水 "})
	check(probe.bubbles.size() == 1, "a timer due event shows one bubble")
	check(str(probe.bubbles[0][0]) == "喝水", "the reminder text is trimmed")
	check(is_equal_approx(float(probe.bubbles[0][1]), 7.0),
		"the reminder uses the configured bubble duration")
	probe._on_service_event("timer", "timer.due",
		{"id": 11, "due_at": "2026-11-13T12:05:00+00:00", "text": "喝水"})
	check(probe.bubbles.size() == 1, "the same reminder does not pop twice")
	probe._on_service_event("timer", "timer.due", {"id": 12, "text": "开会"})
	check(probe.bubbles.size() == 2, "a different timer id still shows")
	# A cancelled timer frees its id; the new reminder is a new due_at.
	probe._on_service_event("timer", "timer.due",
		{"id": 11, "due_at": "2026-11-13T13:05:00+00:00", "text": "新提醒"})
	check(probe.bubbles.size() == 3, "a reused timer id with a new due time still shows")
	check(str(probe.bubbles[2][0]) == "新提醒", "the reused-id reminder shows its own text")
	probe._on_service_event("timer", "timer.due", {"id": 13, "text": "  "})
	check(probe.bubbles.size() == 3, "an empty reminder shows nothing")
	probe._on_service_event("chat", "other.event", {"id": 14, "text": "x"})
	check(probe.bubbles.size() == 3, "unrelated events are ignored")
	probe.free()


## A reminder that arrives while the pet is hidden cannot be seen: the bubble
## canvas is invisible, so the text is queued instead and replayed when the tray
## shows the pet again. The replay collapses into one bubble and is consumed
## once; the dedupe set keeps a repeated event from showing or queueing again.
func _test_timer_event_deferred_while_hidden() -> void:
	var probe := TimerProbe.new()
	probe.bubble = {"enabled": true, "show_replies": true, "duration_seconds": 4}
	probe.hidden = true
	probe._on_service_event("timer", "timer.due", {"id": 21, "text": "喝水"})
	probe._on_service_event("timer", "timer.due", {"id": 22, "text": "开会"})
	check(probe.bubbles.is_empty(), "a reminder while hidden shows no bubble yet")
	check(probe._deferred_timer_bubbles.size() == 2, "hidden reminders are queued")
	probe._flush_deferred_timer_bubbles()
	check(probe.bubbles.is_empty() and probe._deferred_timer_bubbles.size() == 2,
		"flushing while still hidden keeps the queue")
	probe.hidden = false
	probe._flush_deferred_timer_bubbles()
	check(probe.bubbles.size() == 1, "showing the pet replays the queued reminders")
	check(str(probe.bubbles[0][0]) == "喝水；开会",
		"queued reminders share one bubble in arrival order")
	check(is_equal_approx(float(probe.bubbles[0][1]), 4.0),
		"the replay uses the configured bubble duration")
	check(probe._deferred_timer_bubbles.is_empty(), "the replay empties the queue")
	probe._on_service_event("timer", "timer.due", {"id": 21, "text": "喝水"})
	check(probe.bubbles.size() == 1 and probe._deferred_timer_bubbles.is_empty(),
		"a repeated timer.due is neither shown nor queued again")
	probe.free()

	# A long hidden stretch caps the queue: the oldest reminders are dropped
	# rather than showing an unreadable wall of text.
	var burst := TimerProbe.new()
	burst.hidden = true
	for index in range(1, 13):
		burst._on_service_event("timer", "timer.due", {"id": index, "text": "r%d" % index})
	check(burst._deferred_timer_bubbles.size() == 10, "the queue caps at ten reminders")
	check(str(burst._deferred_timer_bubbles[0]) == "r3",
		"the cap drops the oldest reminders first")
	burst.hidden = false
	burst._flush_deferred_timer_bubbles()
	check(burst.bubbles.size() == 1 and str(burst.bubbles[0][0]).begins_with("r3；")
		and not str(burst.bubbles[0][0]).contains("r2"),
		"the replay shows only the kept reminders")
	burst.free()


## The stock transport drops frames with no pending request except
## autonomy.bubble. The app subclass must relay timer.due without duplicating
## the autonomy event the stock client already emits.
func _test_server_event_forwarding() -> void:
	var client = MainWindow.DesktopServiceClient.new()
	var events: Array = []
	client.event_received.connect(func(id: String, type: String, payload: Dictionary):
		events.append([id, type, payload])
	)
	client._dispatch({"id": "timer", "type": "timer.due",
		"payload": {"id": 5, "text": "喝水"}})
	check(events.size() == 1 and str(events[0][1]) == "timer.due",
		"the app client forwards a server-initiated timer.due")
	check(str(events[0][0]) == "timer", "the synthetic timer id is preserved")
	client._dispatch({"id": "autonomy", "type": "autonomy.bubble",
		"payload": {"text": "在的"}})
	check(events.size() == 2 and str(events[1][1]) == "autonomy.bubble",
		"the stock autonomy event is forwarded exactly once")
	client.free()


class FakeWheelBridge extends RefCounted:
	var deltas: Array = []

	func poll_wheel_delta() -> int:
		return int(deltas.pop_front()) if not deltas.is_empty() else 0


class FakeServiceClient extends Node:
	var state := "ready"
	var patches: Array = []
	var _sequence := 0

	func update_settings(patch: Dictionary) -> String:
		patches.append(patch.duplicate(true))
		_sequence += 1
		return "save-%d" % _sequence


## main.gd needs a live client in production; this subclass only records what
## the timer handler would have put into the bubble.
class TimerProbe extends MainWindow:
	var bubbles: Array = []

	func show_bubble(text: String, seconds := -1.0, _reply := false) -> void:
		bubbles.append([text, seconds])
