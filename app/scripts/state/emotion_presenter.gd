extends Node
## Applies the service presentation (emotion label + blend weight) to the pet
## actor. Presentation arrives with chat completions and from periodic
## state.get polls; the last applied value is cached so a fresh actor can be
## set up again without waiting for the service.

const REFRESH_SECONDS := 20.0
const HYSTERESIS := 0.12
const EMOTIONS := ["neutral", "happy", "sad"]
const REQUEST_TIMEOUT := 8.0

var _service
var _host
var _request_id := ""
var _emotion := ""
var _weight := 1.0
var _elapsed := 0.0

func configure(service, host) -> void:
	_service = service
	_host = host
	if _service and _service.has_signal("request_finished") and not _service.request_finished.is_connected(_on_request_finished):
		_service.request_finished.connect(_on_request_finished)

func apply_delta(delta: Dictionary) -> void:
	var value: Variant = delta.get("presentation")
	if value is Dictionary:
		_apply(value)

func refresh() -> void:
	if _service == null or str(_service.get("state")) != "ready":
		return
	var id: String = _service.request("state.get", {}, REQUEST_TIMEOUT)
	# An empty id means the transport refused the request; clear any stale id so a
	# late reply cannot be applied as if it answered this poll.
	_request_id = id

func apply_now() -> bool:
	if _emotion.is_empty():
		return false
	var actor: Variant = _actor()
	if actor == null:
		return false
	var applied := bool(actor.set_emotion(_emotion, _weight))
	if not applied:
		# The new actor cannot render the cached expression. Drop the cache so
		# the next identical presentation is not swallowed by hysteresis.
		_emotion = ""
		_weight = 1.0
	return applied

func _process(delta: float) -> void:
	_elapsed += delta
	if _elapsed < REFRESH_SECONDS:
		return
	_elapsed = 0.0
	refresh()

func _on_request_finished(id: String, type: String, payload: Dictionary) -> void:
	if _request_id.is_empty() or id != _request_id:
		return
	_request_id = ""
	if type != "result":
		return
	var value: Variant = payload.get("presentation")
	if value is Dictionary:
		_apply(value)
		return
	# A state.get reply may also nest the presentation inside the state document.
	var state: Variant = payload.get("state")
	if state is Dictionary:
		var nested: Variant = state.get("presentation")
		if nested is Dictionary:
			_apply(nested)

func _apply(presentation: Dictionary) -> void:
	var emotion := str(presentation.get("emotion", "neutral"))
	if emotion not in EMOTIONS:
		emotion = "neutral"
	var weight: Variant = presentation.get("weight", 1.0)
	if not (weight is int or weight is float):
		weight = 1.0
	elif not is_finite(float(weight)):
		# NaN/INF are not meaningful blends; clamping alone leaves NaN alone.
		weight = 1.0
	var blend := clampf(float(weight), 0.0, 1.0)
	var actor: Variant = _actor()
	if actor == null:
		return
	# Hysteresis compares against what the actor actually shows, not only the
	# cache: reset_performance_state() puts the face back to neutral and the
	# same presentation must be able to restore it.
	if emotion == _actor_emotion(actor) \
			and absf(blend - _actor_weight(actor)) < HYSTERESIS:
		return
	var applied := bool(actor.set_emotion(emotion, blend))
	if not applied and emotion != "neutral":
		# An expression the model cannot render falls back to the neutral face.
		emotion = "neutral"
		applied = bool(actor.set_emotion(emotion, blend))
	if not applied:
		return
	# Only an accepted expression updates the cache.
	_emotion = emotion
	_weight = blend

## The actor's current label, or the cache when the actor exposes no state.
func _actor_emotion(actor: Variant) -> String:
	var value: Variant = actor.get("emotion")
	if value is String and not value.is_empty():
		return value
	return _emotion

## The actor's current blend weight, or the cache when it exposes no state.
func _actor_weight(actor: Variant) -> float:
	var value: Variant = actor.get("emotion_weight")
	if value is int or value is float:
		return float(value)
	return _weight

func _actor() -> Variant:
	if not is_instance_valid(_host):
		return null
	var actor: Variant = _host.get("actor")
	if actor == null or not is_instance_valid(actor) or not actor.has_method("set_emotion"):
		return null
	return actor
