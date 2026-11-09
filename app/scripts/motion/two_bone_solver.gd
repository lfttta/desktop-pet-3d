class_name PetTwoBoneSolver
extends RefCounted

## Two-bone IK math for a leg (hip -> knee -> ankle).
##
## The engine's built-in two-bone modifier reports success but never moves the
## bones in the shipped build, so the solve is explicit here. Everything is a
## pure function of world-space inputs, which keeps it testable.

const EPSILON := 0.000001


## Rotates the chain so the end lands as close to `target` as the segment
## lengths allow. Returns the new world-space mid and end positions.
##
## `pole` biases the knee direction: the knee is kept in the plane spanned by
## the root->target axis and this vector, which prevents the leg from flipping
## sideways when the reach is close to full extension.
static func solve(root: Vector3, mid: Vector3, end: Vector3, target: Vector3, pole: Vector3) -> Dictionary:
	var upper := root.distance_to(mid)
	var lower := mid.distance_to(end)
	if upper <= EPSILON or lower <= EPSILON:
		return {"mid": mid, "end": end, "reachable": false}

	var to_target := target - root
	var distance := to_target.length()
	if distance <= EPSILON:
		distance = EPSILON
		to_target = Vector3(0.0, -1.0, 0.0)
	var direction := to_target / distance
	var reachable := distance <= upper + lower - EPSILON
	var clamped := clampf(distance, absf(upper - lower) + EPSILON, upper + lower - EPSILON)

	# Cosine rule: how far along the axis the knee projects, and how far off it.
	var along := (upper * upper - lower * lower + clamped * clamped) / (2.0 * clamped)
	var height_squared := upper * upper - along * along
	var height := sqrt(maxf(0.0, height_squared))

	# Pick the knee plane. A degenerate pole falls back to the current knee.
	var axis := direction
	var side := pole - axis * pole.dot(axis)
	if side.length_squared() <= EPSILON:
		side = mid - root
		side -= axis * side.dot(axis)
	if side.length_squared() <= EPSILON:
		side = axis.cross(Vector3.RIGHT)
		if side.length_squared() <= EPSILON:
			side = axis.cross(Vector3.FORWARD)
	if side.length_squared() <= EPSILON:
		return {"mid": mid, "end": end, "reachable": false}
	side = side.normalized()

	var new_mid := root + axis * along + side * height
	var new_end := root + axis * clamped
	return {
		"mid": new_mid,
		"end": new_end,
		"reachable": reachable,
		"clamped": clamped,
		"upper": upper,
		"lower": lower,
	}


## Rotation that turns vector `from` onto vector `to`.
static func rotation_between(from: Vector3, to: Vector3) -> Quaternion:
	if from.length_squared() <= EPSILON or to.length_squared() <= EPSILON:
		return Quaternion.IDENTITY
	var a := from.normalized()
	var b := to.normalized()
	var dot := clampf(a.dot(b), -1.0, 1.0)
	if dot > 1.0 - EPSILON:
		return Quaternion.IDENTITY
	if dot < -1.0 + EPSILON:
		var axis := a.cross(Vector3.UP)
		if axis.length_squared() <= EPSILON:
			axis = a.cross(Vector3.RIGHT)
		return Quaternion(axis.normalized(), PI)
	return Quaternion(a, b)
