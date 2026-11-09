extends SceneTree
## Focused tests for the whole-cycle walk plan and the walk sub-mode of the
## motion controller. Both are pure arithmetic, so no display server is needed.

const WalkPlan = preload("res://scripts/behavior/walk_plan.gd")
const MotionController = preload("res://scripts/motion/motion_controller.gd")

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
	_test_plan_shape()
	_test_position_monotonic()
	_test_cycles_that_fit()
	_test_controller_walk()
	_test_controller_interrupts()
	print("WALK_PLAN_TESTS checks=", checks, " failures=", failures)
	quit(1 if failures else 0)


## The plan must cover exactly `cycles` strides and nothing else.
func _test_plan_shape() -> void:
	var plan: Dictionary = WalkPlan.plan(500.0, 1, 3, 100.0, 1.0)
	check(is_equal_approx(plan.start_x, 500.0), "plan keeps the start x")
	check(is_equal_approx(plan.target_x, 800.0), "plan covers 3 strides to the right, got %f" % plan.target_x)
	check(int(plan.cycles) == 3, "plan keeps the cycle count")
	var left: Dictionary = WalkPlan.plan(500.0, -1, 2, 100.0, 1.0)
	check(is_equal_approx(left.target_x, 300.0), "a leftward plan subtracts, got %f" % left.target_x)
	var still: Dictionary = WalkPlan.plan(500.0, 0, 2, 100.0, 1.0)
	check(is_equal_approx(still.target_x, 500.0), "direction 0 does not move")
	# The clamp must not silently raise the count: near an edge the caller sizes
	# it with cycles_that_fit, and a floor of 2 would walk past the wall.
	var one: Dictionary = WalkPlan.plan(0.0, 1, 1, 100.0, 1.0)
	check(is_equal_approx(one.target_x, 100.0), "a single cycle is accepted, got %f" % one.target_x)


## Position must be monotonic and land exactly on the target, with no
## accumulated float drift at the end.
func _test_position_monotonic() -> void:
	var plan: Dictionary = WalkPlan.plan(100.0, 1, 4, 50.0, 1.0)
	check(is_equal_approx(WalkPlan.position_at(plan, 0, 0.0), 100.0), "position at the start is exact")
	check(is_equal_approx(WalkPlan.position_at(plan, 4, 1.0), 300.0), "position at the end is exact, got %f" % WalkPlan.position_at(plan, 4, 1.0))
	var previous := -INF
	var step := 0.05
	var cycles := int(plan.cycles)
	for i in range(cycles * 20 + 1):
		var done := i / 20
		var intra := (i % 20) / 20.0
		var x := WalkPlan.position_at(plan, done, intra)
		check(x >= previous - 0.0001, "position is monotonic at cycle %d intra %f" % [done, intra])
		check(x >= 100.0 - 0.0001 and x <= 300.0 + 0.0001, "position stays inside the plan at %f" % x)
		previous = x
	# Halfway through the second of four cycles is 37.5% of the distance.
	var mid := WalkPlan.position_at(plan, 1, 0.5)
	check(absf(mid - 175.0) < 0.001, "mid-plan progress is proportional, got %f" % mid)
	# Out-of-range inputs are clamped, not extrapolated: the body must never be
	# sent past the wall by a stale frame counter.
	check(is_equal_approx(WalkPlan.position_at(plan, 99, 5.0), 300.0), "overshoot clamps to the target")
	check(is_equal_approx(WalkPlan.position_at(plan, -3, -1.0), 100.0), "undershoot clamps to the start")


## cycles_that_fit must never report a distance the room cannot hold.
func _test_cycles_that_fit() -> void:
	check(WalkPlan.cycles_that_fit(0.0, 100.0, 5) == 0, "no room fits no cycles")
	check(WalkPlan.cycles_that_fit(50.0, 100.0, 5) == 0, "less than one stride fits nothing")
	check(WalkPlan.cycles_that_fit(100.0, 100.0, 5) == 1, "exactly one stride fits one cycle")
	check(WalkPlan.cycles_that_fit(350.0, 100.0, 5) == 3, "three whole strides fit three cycles")
	check(WalkPlan.cycles_that_fit(10000.0, 100.0, 5) == 5, "the wanted count caps the result")
	check(WalkPlan.cycles_that_fit(10000.0, 100.0, 99) == WalkPlan.MAX_CYCLES, "the hard ceiling applies")
	check(WalkPlan.cycles_that_fit(500.0, 0.0, 5) == 0, "a zero stride cannot walk")


## The controller's walk sub-mode: begins only when grounded, advances by
## animation progress, and reports completion exactly once.
func _test_controller_walk() -> void:
	var motion := MotionController.new()
	motion.set_bounds(Rect2i(0, 0, 1000, 500), Vector2(100, 300))
	motion.settle_at(500.0)
	check(not motion.is_walking(), "a settled pet is not walking")
	var plan: Dictionary = motion.begin_walk(1, 3, 100.0, 1.0)
	check(not plan.is_empty(), "a grounded pet can begin a walk")
	check(motion.is_walking(), "the walk is reported as active")
	check(int(plan.cycles) == 3, "the requested cycle count is kept")
	check(motion.walk_cycles_left() == 3, "all cycles are still ahead")
	check(is_equal_approx(motion.anchor.y, 500.0), "walking starts on the ground line")

	check(not motion.advance_walk(0, 0.5), "half of the first cycle is not the end")
	# 3 cycles over 300 px: half of one cycle is a sixth of the distance.
	check(is_equal_approx(motion.anchor.x, 550.0), "half a cycle moves a sixth of the distance, got %f" % motion.anchor.x)
	check(absf(motion.walk_progress() - 1.0 / 6.0) < 0.0001, "progress is cycle-fractional, got %f" % motion.walk_progress())
	check(not motion.advance_walk(2, 0.0), "two of three cycles is not the end")
	check(is_equal_approx(motion.anchor.x, 700.0), "two cycles moved two strides, got %f" % motion.anchor.x)
	var finished: bool = motion.advance_walk(3, 1.0)
	check(finished, "the last cycle reports completion")
	check(is_equal_approx(motion.anchor.x, 800.0), "the body lands exactly on the target, got %f" % motion.anchor.x)
	check(absf(motion.walk_progress() - 1.0) < 0.0001, "a finished walk is at full progress")

	# A walk clamps to the work area, so a plan near the wall cannot leave it.
	motion.settle_at(950.0)
	motion.begin_walk(1, 8, 100.0, 1.0)
	motion.advance_walk(8, 1.0)
	check(motion.anchor.x <= 950.0 + 0.001, "the anchor never leaves the work area, got %f" % motion.anchor.x)


## Anything that takes the body over must cancel a walk outright, and a walk
## must never start while the pet is airborne.
func _test_controller_interrupts() -> void:
	var motion := MotionController.new()
	motion.set_bounds(Rect2i(0, 0, 1000, 500), Vector2(100, 300))
	motion.settle_at(400.0)
	motion.begin_walk(1, 2, 100.0, 1.0)
	check(motion.is_walking(), "walk started")
	motion.begin_hold(Vector2(450.0, 200.0), 100.0)
	check(not motion.is_walking(), "grabbing cancels the walk")

	motion.settle_at(400.0)
	check(motion.begin_walk(1, 2, 100.0, 1.0).is_empty() == false, "walk restarts after settling")
	# Only a real hold can release into a fall; `release()` on a grounded pet is
	# a no-op, so the walk must still be there until the grab actually happens.
	motion.begin_hold(Vector2(450.0, 200.0), 100.0)
	motion.release()
	check(not motion.is_walking(), "releasing into a fall cancels the walk")

	# Airborne pets cannot start walking, and neither can a zero stride.
	var falling := MotionController.new()
	falling.set_bounds(Rect2i(0, 0, 1000, 500), Vector2(100, 300))
	falling.settle_at(400.0)
	falling.begin_hold(Vector2(400.0, 200.0), 100.0)
	falling.release()
	check(falling.begin_walk(1, 2, 100.0, 1.0).is_empty(), "a falling pet cannot begin a walk")

	var still := MotionController.new()
	still.set_bounds(Rect2i(0, 0, 1000, 500), Vector2(100, 300))
	still.settle_at(400.0)
	check(still.begin_walk(1, 2, 0.0, 1.0).is_empty(), "a zero stride cannot begin a walk")
	check(still.begin_walk(0, 2, 100.0, 1.0).is_empty(), "direction 0 cannot begin a walk")
