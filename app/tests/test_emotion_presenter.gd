extends SceneTree
## Emotion presentation wiring: delta application, hysteresis, neutral fallback
## and the periodic state.get refresh.

const EmotionPresenter = preload("res://scripts/state/emotion_presenter.gd")

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
	# Mirrors character_view.gd: the actor reports the face it currently shows.
	var emotion := "neutral"
	var emotion_weight := 1.0
	var calls: Array = []
	var reject: Array = []

	func set_emotion(name_: String, weight := 1.0) -> bool:
		calls.append({"emotion": name_, "weight": weight})
		if name_ in reject:
			return false
		emotion = name_
		emotion_weight = weight
		return true

	func reset_face() -> void:
		# Same effect as character_view.reset_performance_state().
		emotion = "neutral"
		emotion_weight = 1.0
