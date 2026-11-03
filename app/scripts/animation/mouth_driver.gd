class_name PetMouthDriver
extends RefCounted

var _samples := PackedFloat32Array()
var _duration := 0.0


func set_envelope(samples: PackedFloat32Array, duration: float) -> void:
	_samples = samples.duplicate()
	_duration = maxf(0.0, duration)


func sample(playback_position: float, playing: bool) -> float:
	if not playing or _samples.is_empty() or _duration <= 0.0 or playback_position < 0 or playback_position >= _duration:
		return 0.0
	var normalized := clampf(playback_position / _duration, 0.0, 1.0)
	var position := normalized * float(_samples.size() - 1)
	var index := floori(position)
	return clampf(lerpf(_samples[index], _samples[mini(index + 1, _samples.size() - 1)], position - index), 0.0, 1.0)


func stop() -> void:
	_samples = PackedFloat32Array()
	_duration = 0.0
