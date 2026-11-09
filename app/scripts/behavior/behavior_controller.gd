class_name PetBehaviorController
extends RefCounted

## Picks what the pet does next when nothing else is asking for it.
##
## The chain is deliberately coarse: idle, a short ambient action, or a walk.
## Everything here is pure decision logic — no nodes, no timers, no window
## position. The window drives `decide()` when its own cooldown expires and acts
## on the returned decision, which keeps the policy testable without a display
## server and lets the caller stay authoritative about *when* a decision is
## allowed at all (a held, falling, or hidden pet must not decide anything).
##
## Randomness is injected: tests pin the generator and can therefore assert an
## exact branch instead of a statistical distribution.

const WalkPlan = preload("res://scripts/behavior/walk_plan.gd")

## Roll thresholds, evaluated in order. Idle first so a quiet pet stays quiet
## most of the time; walking is the rarer event.
const CHANCE_IDLE := 0.30
const CHANCE_ACTION := 0.40
## The remainder (0.30) walks.

## How long the pet waits before asking for a decision again, in seconds.
const COOLDOWN_IDLE := Vector2(2.5, 6.0)
const COOLDOWN_ACTION := Vector2(0.5, 1.5)
const COOLDOWN_WALK := Vector2(1.0, 3.0)

## Ambient actions must finish inside this budget. Longer clips are excluded:
## a 20 s dance would freeze the behaviour chain and, worse, sit in the way of a
## user who wants to drag the pet somewhere.
const MAX_ACTION_SECONDS := 10.0

## Curated ambient set, in the order of the shipped library. Curated rather than
## purely contract-driven so the pet's idle manners stay predictable; the
## contract check in `ambient_actions()` still runs afterwards as a guard, so an
## action that is removed, lengthened, or turned into a loop drops out instead
## of stalling the chain.
const AMBIENT_ALLOW: Array[String] = [
	"looking_around",
	"thinking",
	"yawn",
	"neck_stretching",
	"happy",
	"surprised",
	"agreeing",
	"shrugging",
	"clapping",
	"cheering",
]

## Actions the behaviour chain must never choose, with the reason kept next to
## the name so a future reader does not "helpfully" re-add one.
const NEVER_AMBIENT := {
	# Physics owns these: pickup/fall are driven by the drag-and-drop state
	# machine, recover by the landing path. Choosing them here would fight it.
	"pickup": "physics",
	"fall": "physics",
	"recover": "physics",
	# Displacement owns these: the window translates only for `walk`.
	"walk": "locomotion",
	"running": "locomotion",
	"old_man_walk": "locomotion",
	"holding_walk": "locomotion",
	"box_walk_arc": "locomotion",
	"running_tired": "locomotion",
	# One-frame pose: its clip is 0.033 s, so it flashes and leaves.
	"male_sitting_pose": "degenerate",
	# Turn rewrites the body yaw; the behaviour chain does not own rotation.
	"turn": "rotation",
	# Poses that depend on a prop or a context the desktop does not provide.
	"climbing": "context",
	"climbing_down_wall": "context",
	"climbing_ladder": "context",
	"climbing_up_wall": "context",
	"laying_nodding": "context",
	"laying_severe_cough": "context",
	"standing_equip_bow": "context",
	"holding_down": "context",
	"carrying": "context",
	"box_idle": "context",
}

## Decision kinds.
const KIND_IDLE := "idle"
const KIND_ACTION := "action"
const KIND_WALK := "walk"

var rng: RandomNumberGenerator

## Optional override for the branch roll, used by tests to pin a specific band
## without sampling. Returns 0..1; the controller falls back to `rng.randf()`.
var roll_override := Callable()

## Highest cycle count a walk may ask for; `cycles_that_fit` lowers it.
var max_walk_cycles := WalkPlan.MAX_CYCLES


func _init(generator: RandomNumberGenerator = null) -> void:
	rng = generator if generator != null else RandomNumberGenerator.new()
	if generator == null:
		rng.randomize()


## Chooses the next behaviour.
##
## `available` maps action name -> {mode?, length?, label?}. `mode` defaults to
## "once" and `length` to 0 when the caller could not supply them; a zero length
## fails the budget check, so an entry with no real clip data is never chosen.
##
## `room_left` / `room_right` are the pixel gaps from the body's horizontal
## centre to each wall of the work area; `stride_px` is one gait cycle's ground
## distance. When neither side has room for a single cycle the walk branch is
## skipped and an action or idle is chosen instead — the pet never plans a move
## it cannot finish.
##
## Returns {kind, action?, direction?, cycles?, cooldown}. `cooldown` is the
## caller's wait before the next call, in seconds.
func decide(available: Dictionary, room_left: float, room_right: float, stride_px: float) -> Dictionary:
	var roll := _roll()
	if roll < CHANCE_IDLE:
		return _idle()
	if roll < CHANCE_IDLE + CHANCE_ACTION:
		var picked := pick_ambient(available)
		if not picked.is_empty():
			return _action(picked, available)
		# Nothing eligible in this library: falling back to idle is honest, and
		# a silent substitution would hide a broken action table.
		return _idle()

	var direction := _pick_direction(room_left, room_right, stride_px)
	if direction == 0:
		# Both sides are walled in. Acting out a short clip is still possible,
		# so try that before giving up and standing still.
		var fallback := pick_ambient(available)
		if not fallback.is_empty():
			return _action(fallback, available)
		return _idle()
	var wanted := rng.randi_range(WalkPlan.MIN_CYCLES, max_walk_cycles)
	var room := room_right if direction > 0 else room_left
	var cycles := WalkPlan.cycles_that_fit(room, stride_px, wanted)
	if cycles <= 0:
		return _idle()
	return {
		"kind": KIND_WALK,
		"direction": direction,
		"cycles": cycles,
		"cooldown": rng.randf_range(COOLDOWN_WALK.x, COOLDOWN_WALK.y),
	}


## The branch roll, 0..1. Honours `roll_override` when a test needs an exact
## band; otherwise it draws from the injected generator.
func _roll() -> float:
	if roll_override.is_valid():
		return clampf(float(roll_override.call()), 0.0, 1.0)
	return rng.randf()


## Ambient actions this library actually offers: the curated list, filtered by
## the contract rules above.
func ambient_actions(available: Dictionary) -> Array[String]:
	var result: Array[String] = []
	for name in AMBIENT_ALLOW:
		if NEVER_AMBIENT.has(name):
			continue
		var info: Variant = available.get(name)
		if not (info is Dictionary):
			continue
		if str(info.get("mode", "once")) != "once":
			# loop/hold clips never end on their own; the chain would stall.
			continue
		var length := float(info.get("length", 0.0))
		if length <= 0.0 or length > MAX_ACTION_SECONDS:
			continue
		result.append(name)
	return result


## Uniformly picks an eligible ambient action, or "" when none qualifies.
func pick_ambient(available: Dictionary) -> String:
	var candidates := ambient_actions(available)
	if candidates.is_empty():
		return ""
	return candidates[rng.randi_range(0, candidates.size() - 1)]


## -1 / +1 for the direction that has room, 0 when neither does.
##
## A side is only usable when it fits at least one full cycle: a partial cycle
## would have to be played at a different rate than the clip, and the soles
## would slide. When both sides fit, the choice is uniform.
func _pick_direction(room_left: float, room_right: float, stride_px: float) -> int:
	var stride := maxf(0.0, float(stride_px))
	if stride <= 0.0:
		return 0
	var can_left := float(room_left) >= stride
	var can_right := float(room_right) >= stride
	if can_left and can_right:
		return -1 if rng.randi_range(0, 1) == 0 else 1
	if can_left:
		return -1
	if can_right:
		return 1
	return 0


func _idle() -> Dictionary:
	return {
		"kind": KIND_IDLE,
		"cooldown": rng.randf_range(COOLDOWN_IDLE.x, COOLDOWN_IDLE.y),
	}


func _action(name: String, available: Dictionary) -> Dictionary:
	# Give the clip a moment to settle afterwards before deciding again, so two
	# unrelated actions cannot butt up against each other and read as a glitch.
	var length := float((available[name] as Dictionary).get("length", 0.0))
	return {
		"kind": KIND_ACTION,
		"action": name,
		"cooldown": maxf(rng.randf_range(COOLDOWN_ACTION.x, COOLDOWN_ACTION.y), 0.0) + length,
	}
