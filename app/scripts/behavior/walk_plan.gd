class_name PetWalkPlan
extends RefCounted

## One walk segment expressed as whole gait cycles.
##
## The window's horizontal position is derived from the animation's own playback
## position, not from the wall clock, so the window advances exactly as fast as
## the feet plant. That only holds if the distance is an integer number of gait
## cycles: any other distance would need a playback rate that drifts against the
## clip and the soles would skate.
##
## Kept free of Node/Qt types so it can be exercised without a display server.

## Preferred span of one autonomous walk. Two is the shortest that reads as
## "walking somewhere" rather than a twitch; eight keeps one decision from
## monopolising the desktop. This is a *preference*: near an edge a single
## cycle is still a legitimate walk, so `plan()` itself accepts 1.
const MIN_CYCLES := 2
const MAX_CYCLES := 8


## Builds a plan covering `cycles` gait cycles from `start_x`.
##
## `stride_px` is the ground distance one cycle covers, `cycle_seconds` the
## clip's own length. Direction is a sign: +1 right, -1 left, 0 for no movement.
##
## The count is clamped to [1, MAX_CYCLES] rather than [MIN_CYCLES, MAX_CYCLES]:
## the caller already sized it with `cycles_that_fit()`, and forcing it back up
## to MIN_CYCLES here would walk the body past the edge the caller just checked.
static func plan(start_x: float, direction: int, cycles: int, stride_px: float, cycle_seconds: float) -> Dictionary:
	var count := clampi(cycles, 1, MAX_CYCLES)
	var stride := maxf(0.0, float(stride_px))
	var step := stride * float(count)
	var sign := 1.0 if direction > 0 else (-1.0 if direction < 0 else 0.0)
	return {
		"start_x": float(start_x),
		"target_x": float(start_x) + step * sign,
		"cycles": count,
		"stride_px": stride,
		"cycle_seconds": maxf(0.001, float(cycle_seconds)),
	}


## Screen x for a plan that has completed `cycles_done` full cycles and is
## `intra_progress` (0..1) through the current one.
##
## Monotonic by construction and exact at both ends: the start returns
## `start_x`, and `cycles_done == cycles` with `intra_progress == 1` returns
## exactly `target_x` — no accumulated float drift, so the pet never stops a
## few pixels short of where the plan said it would.
static func position_at(plan_data: Dictionary, cycles_done: int, intra_progress: float) -> float:
	var start := float(plan_data.get("start_x", 0.0))
	var target := float(plan_data.get("target_x", start))
	var total := maxi(1, int(plan_data.get("cycles", 1)))
	var done := clampi(cycles_done, 0, total)
	var intra := clampf(float(intra_progress), 0.0, 1.0)
	if done >= total:
		return target
	var progress := (float(done) + intra) / float(total)
	return start + (target - start) * progress


## Fraction of the whole plan that is complete. Convience for callers that only
## want to test for the end without unpacking the plan.
static func progress_at(plan_data: Dictionary, cycles_done: int, intra_progress: float) -> float:
	var total := maxi(1, int(plan_data.get("cycles", 1)))
	var done := clampi(cycles_done, 0, total)
	if done >= total:
		return 1.0
	return (float(done) + clampf(float(intra_progress), 0.0, 1.0)) / float(total)


## Largest cycle count whose distance still fits in `room_px`.
##
## Returns 0 when not even one cycle fits: the caller must then refuse the
## movement instead of walking into the wall or sliding the body out of the
## work area. `MIN_CYCLES` is a preference, not a guarantee — near an edge a
## single cycle is still a legitimate walk, so the floor here is 1, and
## `MIN_CYCLES` is only applied by `plan()` when there is room for it.
static func cycles_that_fit(room_px: float, stride_px: float, wanted: int) -> int:
	var stride := maxf(0.0, float(stride_px))
	if stride <= 0.0:
		return 0
	var room := maxf(0.0, float(room_px))
	var fits := int(floor(room / stride))
	if fits <= 0:
		return 0
	return clampi(mini(wanted, fits), 1, MAX_CYCLES)
