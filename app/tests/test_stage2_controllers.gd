extends SceneTree

const ActionController = preload("res://scripts/animation/action_controller.gd")
const ExpressionController = preload("res://scripts/animation/expression_controller.gd")
const MouthDriver = preload("res://scripts/animation/mouth_driver.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	var actions := {
		"idle": {"mode": "loop"}, "walk": {"mode": "loop"}, "wave": {"mode": "once"},
		"pickup": {"mode": "hold"}, "fall": {"mode": "hold"}, "recover": {"mode": "once"}, "turn": {"mode": "once"}
	}
	var controller := ActionController.new()
	controller.configure(null, actions)
	var wave := controller.request_action("wave", "user")
	check(wave > 0 and controller.current_action == "wave", "user wave starts")
	check(controller.request_action("walk", "autonomous") == -1, "lower priority cannot interrupt user action")
	var drag := controller.request_action("pickup", "drag", 1.0, 1, true)
	check(drag > wave and controller.current_action == "pickup", "drag interrupts user action")
	controller.complete(wave)
	check(controller.current_action == "pickup", "stale completion cannot reset newer action")
	controller.release_action(drag)
	check(controller.current_action == "idle", "releasing held pickup returns to idle")
	var recover := controller.request_action("recover", "recovery")
	controller.complete(recover)
	check(controller.current_action == "idle", "one-shot completion returns to idle")
	controller.reset()
	check(controller.current_action == "idle" and controller.current_request > recover, "reset invalidates callbacks")

	var expressions := ExpressionController.new()
	expressions.configure({"expressions": false, "blink": false, "mouth": false, "look": false})
	check(not expressions.set_channel("emotion", "happy", 1.0), "missing expression capability degrades")
	check(expressions.values().is_empty(), "unsupported channels remain neutral")
	expressions.configure({"expressions": true, "blink": true, "mouth": true, "look": false})
	check(expressions.set_channel("emotion", "happy", 0.7), "supported expression is accepted")
	check(expressions.set_channel("blink", "blink", 1.0), "blink channel is independent")
	check(expressions.set_channel("mouth", "mouth_a", 0.5), "mouth channel is independent")
	check(expressions.values().size() == 3, "independent expression channels compose")
	expressions.reset()
	check(expressions.values().is_empty(), "expression reset clears every channel")

	var mouth := MouthDriver.new()
	mouth.set_envelope(PackedFloat32Array([0.0, 0.5, 1.0, 0.0]), 0.3)
	check(is_equal_approx(mouth.sample(0.2, true), 1.0), "mouth samples audio playback position")
	check(mouth.sample(0.2, false) == 0.0, "paused audio closes mouth")
	mouth.stop()
	check(mouth.sample(0.1, true) == 0.0, "stopped audio cannot retain an old envelope")

	print("STAGE2_CONTROLLER_TESTS checks=", checks, " failures=", failures)
	quit(0 if failures == 0 else 1)
