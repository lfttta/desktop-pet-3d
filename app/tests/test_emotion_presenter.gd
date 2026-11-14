extends SceneTree
## Emotion presentation wiring: delta application, hysteresis, neutral fallback,
## the rich-label mapping, emotion-triggered actions (cooldown + idle gate) and
## the periodic state.get refresh.

const EmotionPresenter = preload("res://scripts/state/emotion_presenter.gd")
const CharacterView = preload("res://scripts/character_view.gd")

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
	_test_delta_and_hysteresis()
	_test_neutral_fallback()
	_test_refresh()
	_test_periodic_refresh()
	_test_actor_reset()
	_test_failed_apply_now()
	_test_non_finite_weight()
	_test_missing_actor()
	_test_rich_labels_apply()
	_test_expression_mapping()
	_test_view_expression_fallback()
	_test_action_mapping()
	_test_action_cooldown()
	_test_action_only_when_idle()
	_test_action_not_repeated_per_presentation()
	_test_view_action_gate()
	await _test_real_view_integration()
	print("EMOTION_PRESENTER_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)

func _test_delta_and_hysteresis() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.calls.size() == 1, "a delta with presentation applies one expression")
	check(host.actor.calls[0].emotion == "happy", "delta applies the happy label")
	check(is_equal_approx(float(host.actor.calls[0].weight), 0.8), "delta applies the blend weight")

	# 0.86 - 0.80 = 0.06 < HYSTERESIS (0.12), so the face stays put.
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.86}})
	check(host.actor.calls.size() == 1, "a small weight change within hysteresis is ignored")

	# 0.99 - 0.80 = 0.19 >= HYSTERESIS, so the weight is pushed through.
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.99}})
	check(host.actor.calls.size() == 2, "a weight change beyond hysteresis applies")
	check(is_equal_approx(float(host.actor.calls[1].weight), 0.99), "the new weight reaches the actor")

	presenter.apply_delta({"presentation": {"emotion": "sad", "weight": 0.9}})
	check(host.actor.calls[-1].emotion == "sad", "a different emotion applies regardless of weight")

	presenter.apply_delta({"presentation": {"emotion": "angry", "weight": 0.9}})
	check(host.actor.calls[-1].emotion == "neutral", "an unknown emotion maps to neutral")

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 5.0}})
	check(is_equal_approx(float(host.actor.calls[-1].weight), 1.0), "weights above 1 are clamped")
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": -2.0}})
	check(is_equal_approx(float(host.actor.calls[-1].weight), 0.0), "weights below 0 are clamped")

	presenter.apply_delta({})
	check(host.actor.calls.size() == 6, "a delta without presentation is ignored")
	presenter.apply_delta({"presentation": "happy"})
	check(host.actor.calls.size() == 6, "a presentation that is not a Dictionary is ignored")

	presenter.free()
	host.free()
	service.free()

func _test_neutral_fallback() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.actor.reject = ["happy"]
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.7}})
	check(host.actor.calls.size() == 2, "a rejected expression retries once")
	check(host.actor.calls[0].emotion == "happy", "the rejected attempt uses the requested emotion")
	check(host.actor.calls[1].emotion == "neutral", "the retry falls back to neutral")
	check(host.actor.calls[1].weight == 0.7, "the fallback keeps the requested weight")
	check(presenter._emotion == "neutral", "the cache records the applied neutral fallback")

	# An actor that rejects both the requested face and the neutral fallback
	# must leave the cache untouched.
	host.actor.reject = ["sad", "neutral"]
	var before := presenter._emotion
	var before_weight := presenter._weight
	presenter.apply_delta({"presentation": {"emotion": "sad", "weight": 0.4}})
	check(host.actor.calls[-1].emotion == "neutral", "an unreachable expression ends on neutral")
	check(presenter._emotion == before and presenter._weight == before_weight,
		"the cache is not updated when nothing was applied")

	# apply_now replays the last accepted expression onto the current actor.
	host.actor.reject = []
	var before_count: int = host.actor.calls.size()
	presenter.apply_now()
	check(host.actor.calls.size() == before_count + 1, "apply_now pushes the cached expression")
	check(host.actor.calls[-1].emotion == presenter._emotion, "apply_now reuses the cached emotion")

	presenter.free()
	host.free()
	service.free()

func _test_refresh() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.refresh()
	check(service.requests.size() == 1, "refresh sends one request")
	check(service.requests[0].type == "state.get", "refresh uses state.get")
	check(is_equal_approx(float(service.requests[0].timeout), 8.0), "refresh requests an 8 second timeout")
	check(presenter._request_id == service.requests[0].id, "refresh records the request id")

	service.reply(service.requests[0].id, "result", {"presentation": {"emotion": "happy", "weight": 0.9}})
	check(host.actor.calls.size() == 1 and host.actor.calls[0].emotion == "happy",
		"a state.get result applies its presentation")

	presenter.refresh()
	service.reply(service.requests[1].id, "result", {
		"state": {"presentation": {"emotion": "sad", "weight": 0.6}},
	})
	check(host.actor.calls[-1].emotion == "sad", "a presentation nested under state is applied")

	# Errors and stale ids never touch the actor.
	var before: int = host.actor.calls.size()
	presenter.refresh()
	service.reply(service.requests[2].id, "error", {"code": "not_available", "message": "未接入"})
	check(host.actor.calls.size() == before, "a failed state.get is ignored")
	service.reply("stale-id", "result", {"presentation": {"emotion": "happy", "weight": 0.5}})
	check(host.actor.calls.size() == before, "a reply for another request is ignored")

	service.state = "starting"
	presenter.refresh()
	check(service.requests.size() == 3, "refresh stays idle while the service is not ready")

	# A refused request clears the tracked id so a stale reply cannot apply.
	service.state = "ready"
	service.refuse = true
	presenter.refresh()
	check(presenter._request_id == "", "a refused request clears the tracked id")
	service.reply("fake-9", "result", {"presentation": {"emotion": "happy", "weight": 0.5}})
	check(host.actor.calls.size() == before, "a reply after a refused request is ignored")

	presenter.free()
	host.free()
	service.free()

func _test_periodic_refresh() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	# The engine calls _process every frame; drive it directly so the 20 second
	# period is asserted instead of being replaced by a manual refresh().
	# Literal deltas (not REFRESH_SECONDS) keep the assertion mutation-sensitive.
	presenter._process(19.0)
	check(service.requests.is_empty(), "the periodic tick waits for the refresh interval")
	presenter._process(1.5)
	check(service.requests.size() == 1, "the periodic tick refreshes after the interval")
	check(not service.requests.is_empty() and service.requests[0].type == "state.get",
		"the periodic tick uses state.get")
	presenter._process(20.0)
	check(service.requests.size() == 2, "the periodic tick repeats on the next interval")

	presenter.free()
	host.free()
	service.free()

func _test_actor_reset() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.calls.size() == 1, "the first presentation applies")
	# reset_performance_state() (tray hide) puts the face back to neutral while
	# the presenter still caches happy/0.8.
	host.actor.reset_face()
	var before: int = host.actor.calls.size()
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.calls.size() == before + 1,
		"the same presentation is re-applied after the actor resets itself")

	presenter.free()
	host.free()
	service.free()

func _test_failed_apply_now() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(presenter._emotion == "happy", "the cache records the applied emotion")
	# A replacement actor that cannot render the cached face must not leave the
	# cache authoritative, or hysteresis would swallow the same presentation.
	host.actor.queue_free()
	host.actor = FakeActor.new()
	host.actor.reject = ["happy", "neutral"]
	host.add_child(host.actor)
	var applied := presenter.apply_now()
	check(applied == false, "apply_now reports a rejected replay")
	check(presenter._emotion == "", "a rejected replay clears the cache")
	host.actor.reject = []
	var before: int = host.actor.calls.size()
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.calls.size() > before, "a rejected replay does not stall later presentations")

	presenter.free()
	host.free()
	service.free()

func _test_non_finite_weight() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": NAN}})
	check(host.actor.calls.size() == 1, "a NaN weight still applies the emotion")
	check(is_finite(float(host.actor.calls[0].weight)), "a NaN weight is replaced by a finite fallback")
	check(is_finite(presenter._weight), "a NaN weight is never cached")

	presenter.free()
	host.free()
	service.free()

func _test_missing_actor() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.7}})
	presenter.apply_now()
	check(presenter._emotion == "", "an empty actor leaves the cache empty")

	# An actor without set_emotion is skipped instead of crashing.
	host.actor = Node.new()
	host.add_child(host.actor)
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.7}})
	check(presenter._emotion == "", "an actor without set_emotion is skipped")

	presenter.free()
	host.free()
	service.free()

func _test_rich_labels_apply() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	for emotion in ["excited", "tired", "upset"]:
		presenter.apply_delta({"presentation": {"emotion": emotion, "weight": 0.7}})
		check(host.actor.calls[-1].emotion == emotion,
			"a known rich label reaches the actor: " + emotion)
		check(is_equal_approx(float(host.actor.calls[-1].weight), 0.7),
			"a rich label keeps its blend weight: " + emotion)
		check(presenter._emotion == emotion, "the cache records the rich label: " + emotion)

	# An unknown label still maps to neutral rather than to a new face.
	presenter.apply_delta({"presentation": {"emotion": "angry", "weight": 0.7}})
	check(host.actor.calls[-1].emotion == "neutral", "an unknown label still maps to neutral")

	presenter.free()
	host.free()
	service.free()

func _test_expression_mapping() -> void:
	check(CharacterView.expression_for_label("neutral") == "neutral", "neutral renders neutral")
	check(CharacterView.expression_for_label("happy") == "happy", "happy renders happy")
	check(CharacterView.expression_for_label("sad") == "sad", "sad renders sad")
	check(CharacterView.expression_for_label("excited") == "happy", "excited renders through happy")
	check(CharacterView.expression_for_label("tired") == "sad", "tired renders through sad")
	check(CharacterView.expression_for_label("upset") == "sad", "upset renders through sad")
	check(CharacterView.expression_for_label("angry") == "neutral", "an unknown label falls back to neutral")
	check(CharacterView.expression_for_label("") == "neutral", "an empty label falls back to neutral")
	# The mapping must not invent a face the model does not ship.
	var faces := {}
	for emotion in CharacterView.EXPRESSION_BY_EMOTION:
		faces[CharacterView.expression_for_label(emotion)] = true
	check(faces.keys().all(func(face): return face in ["neutral", "happy", "sad"]),
		"every label maps onto an existing expression")

func _test_view_expression_fallback() -> void:
	# A minimal view without a loaded model still validates labels against its
	# expression config, which is the only fallback the presenter relies on.
	var view := CharacterView.new()
	view.config = {"expressions": {"neutral": [], "happy": [], "sad": []}}
	var seen := []
	for emotion in ["excited", "tired", "upset"]:
		var applied: bool = view.set_emotion(emotion, 0.5)
		check(applied, "the view accepts rich label " + emotion)
		seen.append(view.emotion)
		check(CharacterView.expression_for_label(view.emotion) in ["happy", "sad"],
			"rich label " + emotion + " resolves to an existing face")
	check(not view.set_emotion("angry"), "the view rejects an unknown label")
	check(view.emotion == seen[-1], "a rejected label does not change the face")
	# The neutral fallback path the presenter uses must always be accepted.
	check(view.set_emotion("neutral"), "the view accepts neutral for the fallback retry")
	view.free()

func _test_action_mapping() -> void:
	check(EmotionPresenter.EMOTION_ACTIONS.get("happy") == "cheering", "happy suggests cheering")
	check(EmotionPresenter.EMOTION_ACTIONS.get("sad") == "sad_idle", "sad suggests sad_idle")
	check(EmotionPresenter.EMOTION_ACTIONS.get("tired") == "sleeping_idle", "tired suggests sleeping_idle")
	check(EmotionPresenter.EMOTION_ACTIONS.get("excited") == "cheering", "excited suggests cheering")
	check(EmotionPresenter.EMOTION_ACTIONS.get("upset") == "shrugging", "upset suggests shrugging")
	check(not EmotionPresenter.EMOTION_ACTIONS.has("neutral"), "neutral suggests no action")

func _test_action_cooldown() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions == ["cheering"], "a happy presentation plays cheering")
	check(is_equal_approx(presenter.action_cooldown("cheering"), 30.0),
		"the played action starts a 30 second cooldown")
	# `cheering` is a one-shot; the real view returns to idle when the clip ends.
	host.actor.finish_action()

	# Leaving and returning to the same label inside the cooldown must not replay.
	presenter.apply_delta({"presentation": {"emotion": "sad", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle"], "sad plays its own action")
	host.actor.finish_action()
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle"],
		"returning to happy inside the cooldown does not replay cheering")

	# A different emotion that shares cheering shares the same cooldown.
	presenter.apply_delta({"presentation": {"emotion": "excited", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle"],
		"excited shares the cheering cooldown")

	# 29.9 s is still inside the cooldown...
	presenter._process(29.9)
	check(presenter.action_cooldown("cheering") > 0.0, "the cooldown is still running at 29.9 s")
	# A label transition to an action that is still cooling down is swallowed:
	# leaving and returning must not replay the clip inside the 30 s window.
	presenter.apply_delta({"presentation": {"emotion": "upset", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle", "shrugging"],
		"a label change plays its own action while cheering cools down")
	host.actor.finish_action()
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle", "shrugging"],
		"returning to happy inside the cooldown does not replay cheering")
	# ...and crossing 30 s releases it, so the next label change may replay.
	presenter._process(0.2)
	check(presenter.action_cooldown("cheering") == 0.0, "the cooldown expires after 30 s")
	presenter.apply_delta({"presentation": {"emotion": "excited", "weight": 0.8}})
	check(host.actor.actions == ["cheering", "sad_idle", "shrugging", "cheering"],
		"the action is available again after the 30 second cooldown")

	presenter.free()
	host.free()
	service.free()

func _test_action_only_when_idle() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.actor.action = "wave"
	host.actor.action_source = "user"
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.face_calls == 1, "the face still applies while the pet is busy")
	check(host.actor.actions.is_empty(), "a user action keeps its clip; no emotion action plays")

	# The label was not consumed, so the next presentation while idle may play it.
	host.actor.action = "idle"
	host.actor.action_source = "idle"
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions == ["cheering"],
		"a deferred suggestion plays once the pet is idle again")

	# A chat reply owns the body the same way.
	host.actor.action = "laughing"
	host.actor.action_source = "chat"
	presenter.apply_delta({"presentation": {"emotion": "sad", "weight": 0.8}})
	check(host.actor.actions == ["cheering"], "a chat action keeps its clip too")

	presenter.free()
	host.free()
	service.free()

func _test_action_not_repeated_per_presentation() -> void:
	var service := FakeService.new()
	var host := FakeHost.new()
	root.add_child(service)
	root.add_child(host)
	host.actor = FakeActor.new()
	host.add_child(host.actor)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions.size() == 1, "the first happy presentation plays an action")
	# Repeating the identical presentation is swallowed by hysteresis, so no
	# action is retriggered; this is the per-frame / per-poll guard.
	for i in 5:
		presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.8}})
	check(host.actor.actions.size() == 1, "an identical presentation does not replay the action")
	# A weight-only change still keeps the same face and must not replay either.
	presenter.apply_delta({"presentation": {"emotion": "happy", "weight": 0.95}})
	check(host.actor.actions.size() == 1, "a weight-only change does not replay the action")

	presenter.free()
	host.free()
	service.free()

func _test_view_action_gate() -> void:
	# The real view's gate is asserted without loading a model: `action`,
	# `paused`, an in-flight speech clip and the process mode are what it reads.
	var view := CharacterView.new()
	view.config = {"expressions": {"neutral": [], "happy": [], "sad": []}}
	root.add_child(view)
	check(view.action_available_for_emotion(), "an idle, unpaused view accepts a suggestion")
	view.action = "wave"
	check(not view.action_available_for_emotion(), "a non-idle action blocks a suggestion")
	view.action = "idle"
	view.paused = true
	check(not view.action_available_for_emotion(), "a paused performance blocks a suggestion")
	view.paused = false
	check(view.action_available_for_emotion(), "unpausing restores the gate")
	# The tray hides the pet by disabling its process mode; a suggestion must not
	# start while the body cannot advance its own animation.
	view.process_mode = Node.PROCESS_MODE_DISABLED
	check(not view.action_available_for_emotion(), "a disabled (hidden) view blocks a suggestion")
	view.process_mode = Node.PROCESS_MODE_INHERIT
	check(view.action_available_for_emotion(), "re-enabling the process mode restores the gate")
	# A per-frame hold update on a view with no model is a safe no-op.
	view._update_suggested_action(1.0)
	check(view.current_suggestion() == "", "no pending suggestion on a fresh view")
	view.free()

## End-to-end over the real character view: the presenter applies a rich label,
## the view renders it through an existing face, and the suggested clip really
## starts (then returns to a usable state). This is the check the fakes above
## cannot give: that `suggest_action` is wired to real clips.
func _test_real_view_integration() -> void:
	var view := CharacterView.new()
	root.add_child(view)
	await process_frame
	check(view.load_character({"config": "res://assets_data/godette.json"}),
		"the real character view loads")
	await process_frame
	var host := FakeHost.new()
	host.actor = view
	root.add_child(host)
	var service := FakeService.new()
	root.add_child(service)
	var presenter := EmotionPresenter.new()
	root.add_child(presenter)
	presenter.configure(service, host)

	# The weight is deliberately different from neutral's 1.0 so hysteresis
	# cannot swallow the first presentation.
	presenter.apply_delta({"presentation": {"emotion": "excited", "weight": 0.8}})
	check(view.emotion == "excited", "the real view keeps the rich label")
	check(view.action == "cheering", "excited really starts the cheering clip")
	check(is_equal_approx(presenter.action_cooldown("cheering"), 30.0),
		"the real view's cheering starts the cooldown")

	# Wait for the one-shot to finish so the view is idle again, then show a
	# label whose clip loops. The clip is 2.9 s; the budget is generous so a slow
	# headless frame cannot turn this into a flake.
	await _wait_until(func(): return view.action == "idle", 10.0)
	check(view.action == "idle", "the cheering clip returns the view to idle")
	presenter.apply_delta({"presentation": {"emotion": "sad", "weight": 0.8}})
	check(view.action == "sad_idle", "sad really starts the sad_idle clip")
	check(view.action_available_for_emotion() == false, "a running clip closes the idle gate")
	# `sad_idle` loops, so the view holds it and releases after the hold.
	view._update_suggested_action(CharacterView.EMOTION_ACTION_HOLD_SECONDS + 0.1)
	check(view.action == "idle", "the held loop returns to idle when its hold expires")
	check(view.emotion == "sad", "ending the loop leaves the face alone")
	check(presenter.action_cooldown("sad_idle") > 0.0, "the loop's action is also on cooldown")

	# Face blending still resolves the rich label through the existing shape.
	view.update_face()
	check(view.expression_for_label("excited") == "happy", "the real view resolves excited to happy")
	check(view.expression_for_label("tired") == "sad", "the real view resolves tired to sad")

	# The face shows through the existing blend shapes rather than a new one.
	check(view.set_emotion("upset", 0.5), "the real view accepts upset")
	check(view.expression_for_label(view.emotion) == "sad", "upset renders through sad")
	check(view.set_emotion("neutral", 1.0), "the real view accepts neutral again")

	presenter.free()
	service.free()
	host.free()
	view.free()
	await process_frame

## Polls `condition` until true or `seconds` elapse, advancing the tree.
func _wait_until(condition: Callable, seconds: float) -> void:
	var deadline := Time.get_ticks_msec() + int(seconds * 1000.0)
	while Time.get_ticks_msec() < deadline:
		await process_frame
		if bool(condition.call()):
			return

class FakeService extends Node:
	signal request_finished(id: String, type: String, payload: Dictionary)
	signal event_received(id: String, type: String, payload: Dictionary)
	signal capabilities_received(features: Dictionary, personas: Array)
	var state := "ready"
	var requests: Array = []
	var refuse := false
	var _sequence := 0

	func request(type: String, payload: Dictionary, timeout := 8.0) -> String:
		if refuse:
			return ""
		_sequence += 1
		var id := "fake-%d" % _sequence
		requests.append({"id": id, "type": type, "payload": payload, "timeout": timeout})
		return id

	func reply(id: String, type: String, payload: Dictionary) -> void:
		request_finished.emit(id, type, payload)

class FakeHost extends Node:
	var actor

class FakeActor extends Node:
	# Mirrors character_view.gd: the actor reports the face it currently shows,
	# whether an emotion action may start, and records each accepted one.
	var emotion := "neutral"
	var emotion_weight := 1.0
	var action := "idle"
	var action_source := "idle"
	var paused := false
	var calls: Array = []
	var face_calls := 0
	var actions: Array = []
	var reject: Array = []

	func set_emotion(name_: String, weight := 1.0) -> bool:
		calls.append({"emotion": name_, "weight": weight})
		face_calls += 1
		if name_ in reject:
			return false
		emotion = name_
		emotion_weight = weight
		return true

	func action_available_for_emotion() -> bool:
		return action == "idle" and not paused

	func suggest_action(name_: String) -> bool:
		if not action_available_for_emotion():
			return false
		actions.append(name_)
		action = name_
		action_source = "autonomous"
		return true

	## Mirrors character_view.gd: a one-shot ends through `_animation_finished`.
	func finish_action() -> void:
		action = "idle"
		action_source = "idle"

	func reset_face() -> void:
		# Same effect as character_view.reset_performance_state().
		emotion = "neutral"
		emotion_weight = 1.0
		action = "idle"
		action_source = "idle"
