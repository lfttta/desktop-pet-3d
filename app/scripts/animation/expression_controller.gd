class_name PetExpressionController
extends RefCounted

var _capabilities := {}
var _channels := {}


func configure(capabilities: Dictionary) -> void:
	_capabilities = capabilities.duplicate(true)
	reset()


func set_channel(channel: String, expression: String, weight: float) -> bool:
	var capability: String = {"emotion": "expressions", "blink": "blink", "mouth": "mouth", "look": "look"}.get(channel, "")
	if capability.is_empty() or not bool(_capabilities.get(capability, false)):
		return false
	_channels[channel] = {"expression": expression, "weight": clampf(weight, 0.0, 1.0)}
	return true


func values() -> Dictionary:
	var result := {}
	for value: Dictionary in _channels.values():
		result[str(value.expression)] = maxf(float(result.get(str(value.expression), 0.0)), float(value.weight))
	return result


func reset() -> void:
	_channels.clear()
