extends SceneTree
## Focused tests for the autonomous behaviour policy. The controller is pure
## decision logic with an injectable RNG, so every branch can be pinned exactly
## rather than sampled.

const Behavior = preload("res://scripts/behavior/behavior_controller.gd")
const WalkPlan = preload("res://scripts/behavior/walk_plan.gd")

var checks := 0
var failures := 0


func check(value: bool, label: String) -> void:
	checks += 1
	if not value:
		failures += 1
		push_error(label)


func _initialize() -> void:
	call_deferred("run")


func run() -> void:
	_test_ambient_filter()
	_test_roll_branches()
	_test_edge_gating()
	_test_walk_sizing()
	_test_cooldowns()
	_test_walk_disabled()
	print("BEHAVIOR_CONTROLLER_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)


## A small library standing in for the shipped one: one usable action, plus one
## entry for every rule that must be rejected.
func _library() -> Dictionary:
	return {
		"thinking": {"mode": "once", "length": 4.2},
		"looking_around": {"mode": "once", "length": 6.4},
		"happy": {"mode": "once", "length": 10.0},
		# Rejected: loops never end, so the chain would stall on them.
		"walk": {"mode": "loop", "length": 1.03},
		"idle": {"mode": "loop", "length": 8.33},
		# Rejected: physics owns these.
		"pickup": {"mode": "hold", "length": 0.4},
		"fall": {"mode": "hold", "length": 4.5},
		"recover": {"mode": "once", "length": 0.7},
		# Rejected: too long to play unattended.
		"dancing": {"mode": "once", "length": 21.4},
		# Rejected: a 0.033 s flash.
		"male_sitting_pose": {"mode": "once", "length": 0.033},
		# Rejected: no clip metadata at all.
		"ghost": {"mode": "once", "length": 0.0},
	}


func _test_ambient_filter() -> void:
	var behavior := _pinned(0.0)
	var available := behavior.ambient_actions(_library())
	check(available.has("thinking"), "a short one-shot action is eligible")
	check(available.has("looking_around"), "another short action is eligible")
	check(available.has("happy"), "an action exactly at the budget is eligible")
	check(not available.has("walk"), "a loop clip is rejected")
	check(not available.has("idle"), "the idle loop is rejected")
	check(not available.has("pickup"), "a physics hold is rejected")
	check(not available.has("fall"), "the fall hold is rejected")
	check(not available.has("recover"), "the recovery clip is rejected")
	check(not available.has("dancing"), "a 21 s dance is rejected")
	check(not available.has("male_sitting_pose"), "a one-frame pose is rejected")
	check(not available.has("ghost"), "an action with no length is rejected")
	check(available.size() == 3, "exactly three actions survive the filter, got %d" % available.size())

	# An empty or metadata-free library yields nothing rather than crashing.
	var empty := _pinned(0.0)
	check(empty.ambient_actions({}).is_empty(), "an empty library offers no action")
	check(empty.pick_ambient({}).is_empty(), "picking from an empty library returns an empty name")
	check(empty.ambient_actions({"walk": {"mode": "loop"}}).is_empty(), "a loop-only library offers no action")


## Each roll band must route to its own branch. The RNG is pinned per test.
func _test_roll_branches() -> void:
	# 0.10 is below CHANCE_IDLE (0.30): idle.
	var idle := _pinned(0.10)
	var decision := idle.decide(_library(), 1000.0, 1000.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_IDLE, "a low roll idles, got %s" % decision.kind)

	# 0.50 sits in the action band (0.30..0.70).
	var acting := _pinned(0.50)
	decision = acting.decide(_library(), 1000.0, 1000.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_ACTION, "a middle roll picks an action, got %s" % decision.kind)
	check(str(decision.action) in ["thinking", "looking_around", "happy"], "the action comes from the allow list, got %s" % decision.action)

	# 0.90 is past 0.70: walk.
	var walking := _pinned(0.90)
	decision = walking.decide(_library(), 1000.0, 1000.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_WALK, "a high roll walks, got %s" % decision.kind)
	check(int(decision.direction) != 0, "a walk has a direction")
	check(int(decision.cycles) >= 1, "a walk covers at least one cycle")


## The roll band boundaries must not be off by one.
func _test_edge_gating() -> void:
	# Neither side fits a whole stride: no walk is possible even on a walk roll.
	var boxed := _pinned(0.99)
	var decision := boxed.decide(_library(), 50.0, 50.0, 100.0)
	check(str(decision.kind) != Behavior.KIND_WALK, "a pet with no room does not walk, got %s" % decision.kind)

	# Same roll, but an empty library: the walk fallback has nowhere to go.
	var bare := _pinned(0.99)
	decision = bare.decide({}, 50.0, 50.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_IDLE, "no room and no actions means standing still, got %s" % decision.kind)

	# Only the left side has room.
	var left_only := _pinned(0.99)
	decision = left_only.decide({}, 1000.0, 10.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_WALK, "the open side allows a walk")
	check(int(decision.direction) == -1, "the pet walks towards the only open side, got %d" % decision.direction)

	# Only the right side has room.
	var right_only := _pinned(0.99)
	decision = right_only.decide({}, 10.0, 1000.0, 100.0)
	check(int(decision.direction) == 1, "the pet walks right when only the right is open, got %d" % decision.direction)

	# A zero stride cannot produce a walk at all.
	var broken := _pinned(0.99)
	decision = broken.decide({}, 1000.0, 1000.0, 0.0)
	check(str(decision.kind) != Behavior.KIND_WALK, "a zero stride cannot walk, got %s" % decision.kind)


## A walk may never ask for more cycles than the room holds.
func _test_walk_sizing() -> void:
	var sized := 0
	for i in 200:
		# Real RNG here on purpose: this test is about the sizing of whatever
		# walk the controller does produce, so the roll must be free to vary.
		var behavior := Behavior.new(_fixed_rng(0.95))
		behavior.rng.seed = i
		var room := float(50 + (i * 37) % 400)
		var decision := behavior.decide({}, room, room, 100.0)
		if str(decision.kind) != Behavior.KIND_WALK:
			continue
		var cycles := int(decision.cycles)
		check(cycles * 100.0 <= room + 0.001, "a walk fits its room: %d cycles in %f px" % [cycles, room])
		check(cycles <= WalkPlan.MAX_CYCLES, "a walk never exceeds the ceiling")
		check(cycles >= 1, "a walk covers at least one cycle")
		sized += 1
	check(sized > 0, "at least one walk decision was produced for sizing")


## Cooldowns must be usable by a caller that only counts down seconds.
func _test_cooldowns() -> void:
	var behavior := _pinned(0.50)
	var decision := behavior.decide(_library(), 1000.0, 1000.0, 100.0)
	var cooldown := float(decision.cooldown)
	check(cooldown > 0.0, "an action reports a positive cooldown, got %f" % cooldown)
	# The clip length is included, so two actions cannot butt up together.
	var length := float((_library()[str(decision.action)] as Dictionary).length)
	check(cooldown >= length, "the cooldown covers the clip, got %f for a %f s clip" % [cooldown, length])

	var idle := _pinned(0.10)
	check(float(idle.decide(_library(), 1000.0, 1000.0, 100.0).cooldown) > 0.0, "idle reports a positive cooldown")
	var walk := _pinned(0.90)
	check(float(walk.decide(_library(), 1000.0, 1000.0, 100.0).cooldown) > 0.0, "a walk reports a positive cooldown")


## `walk_enabled` (settings schema v6) removes only the walk branch. A walk roll
## must fall back to an ambient action, or to idle when nothing is eligible —
## exactly what a pet with no room does.
func _test_walk_disabled() -> void:
	# Baseline: the same pin walks while the switch is on.
	var walking := _pinned(0.90)
	check(walking.walk_enabled, "walking is enabled by default")
	var decision := walking.decide(_library(), 1000.0, 1000.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_WALK, "the walk band walks when enabled, got %s" % decision.kind)

	# Switched off: the walk band falls back to an action instead.
	var still := _pinned(0.90)
	still.walk_enabled = false
	decision = still.decide(_library(), 1000.0, 1000.0, 100.0)
	check(str(decision.kind) != Behavior.KIND_WALK, "a disabled walk band never walks, got %s" % decision.kind)
	check(str(decision.kind) == Behavior.KIND_ACTION, "it falls back to an ambient action, got %s" % decision.kind)
	check(str(decision.action) in ["thinking", "looking_around", "happy"], "the fallback action comes from the allow list, got %s" % decision.action)

	# Switched off with nothing to act out: standing still, never a walk.
	var bare := _pinned(0.90)
	bare.walk_enabled = false
	decision = bare.decide({}, 1000.0, 1000.0, 100.0)
	check(str(decision.kind) == Behavior.KIND_IDLE, "no action available means idle, got %s" % decision.kind)

	# Room is irrelevant while walking is off: wide-open space still cannot walk.
	var roomy := _pinned(0.90)
	roomy.walk_enabled = false
	decision = roomy.decide(_library(), 5000.0, 5000.0, 100.0)
	check(str(decision.kind) != Behavior.KIND_WALK, "plenty of room does not re-enable walking, got %s" % decision.kind)

	# The other bands are untouched by the switch.
	var idle := _pinned(0.10)
	idle.walk_enabled = false
	check(str(idle.decide(_library(), 1000.0, 1000.0, 100.0).kind) == Behavior.KIND_IDLE, "the idle band still idles with walking off")
	var acting := _pinned(0.50)
	acting.walk_enabled = false
	check(str(acting.decide(_library(), 1000.0, 1000.0, 100.0).kind) == Behavior.KIND_ACTION, "the action band still acts with walking off")


## RNG with a fixed branch roll. The controller exposes `roll_override` for
## exactly this purpose: overriding `randf()` is rejected by Godot because it is
## a native method, so pinning through the callable is the supported route.
func _fixed_rng(value: float) -> RandomNumberGenerator:
	var rng := RandomNumberGenerator.new()
	rng.seed = 12345
	return rng


## Controller whose branch roll is pinned to `value`.
func _pinned(value: float) -> Behavior:
	var behavior := Behavior.new(_fixed_rng(value))
	behavior.roll_override = func(): return value
	return behavior
