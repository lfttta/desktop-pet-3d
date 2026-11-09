class_name PetLegIK
extends SkeletonModifier3D

## Two-bone leg correction for the shipped VRM characters.
##
## Godot 4.7.2 ships TwoBoneIK3D, but in this build it reports a valid setup and
## leaves the bones untouched (verified on both a synthetic skeleton and
## godette.vrm). A custom SkeletonModifier3D does take effect, and
## set_bone_global_pose_override lands the end bone to within 1e-7 m, so the
## solve is done here on top of whatever the AnimationPlayer produced.
##
## The modifier only corrects what it is told to: with no foot request it does
## nothing at all, so the 65 shared actions play back unchanged.

const Solver = preload("res://scripts/motion/two_bone_solver.gd")

var skeleton: Skeleton3D

## side -> {upper, lower, foot}; bone indices resolved from the contract.
var _chains := {}
## side -> {active, y, weight}: a foot request in world space.
var _targets := {}
var _weight := 0.0
var _character_scale := 1.0


func configure(skel: Skeleton3D, bone_map: Dictionary, character_scale := 1.0) -> void:
	skeleton = skel
	_character_scale = maxf(0.0001, character_scale)
	_chains.clear()
	_targets.clear()
	for side in ["Left", "Right"]:
		var upper := _resolve(bone_map, side + "UpperLeg")
		var lower := _resolve(bone_map, side + "LowerLeg")
		var foot := _resolve(bone_map, side + "Foot")
		if upper < 0 or lower < 0 or foot < 0:
			continue
		_chains[side] = {
			"upper": upper,
			"lower": lower,
			"foot": foot,
		}
		_targets[side] = {"active": false, "y": 0.0, "weight": 0.0}


func _resolve(bone_map: Dictionary, profile: String) -> int:
	if skeleton == null or not bone_map.has(profile):
		return -1
	return skeleton.find_bone(str(bone_map[profile]))


func has_chains() -> bool:
	return not _chains.is_empty()


func chain_count() -> int:
	return _chains.size()


## Global strength of the correction, 0..1. Use it to fade the correction in
## and out so a landing does not pop.
func set_weight(value: float) -> void:
	_weight = clampf(value, 0.0, 1.0)
	if _weight <= 0.0:
		reset_overrides()


func get_weight() -> float:
	return _weight


## Locks a foot at a world-space height (metres, scene space). `weight` fades a
## single side independently, which is what a landing does per foot.
func set_foot_floor(side: String, world_y: float, weight := 1.0) -> void:
	if not _targets.has(side):
		return
	_targets[side] = {"active": true, "y": world_y, "weight": clampf(weight, 0.0, 1.0)}


func clear_foot(side: String) -> void:
	if _targets.has(side):
		_targets[side] = {"active": false, "y": 0.0, "weight": 0.0}
		if skeleton != null and _chains.has(side):
			for bone in [_chains[side].upper, _chains[side].lower, _chains[side].foot]:
				_clear_override(bone)


func clear_feet() -> void:
	for side in _targets:
		clear_foot(side)


func is_foot_locked(side: String) -> bool:
	var target: Dictionary = _targets.get(side, {})
	return bool(target.get("active", false)) and float(target.get("weight", 0.0)) * _weight > 0.0


## Locks both feet at the height the animation currently places them, minus
## `drop`. This is the "hold the soles where they are" form of contact: it keeps
## the pose the animation authored instead of forcing a fixed height that only
## suits the idle stance.
func lock_current_height(drop := 0.0) -> void:
	if skeleton == null:
		return
	for side in _chains:
		var foot: int = _chains[side].foot
		var height := (skeleton.global_transform * skeleton.get_bone_global_pose(foot).origin).y
		_targets[side] = {"active": true, "y": height - drop, "weight": 1.0}


## Re-reads the current ankle heights as the lock target. Call it after an action
## change so the new pose becomes the reference instead of the previous one.
func refresh_lock_heights(drop := 0.0) -> void:
	var active := false
	for side in _targets:
		if bool(_targets[side].get("active", false)):
			active = true
			break
	if not active:
		return
	lock_current_height(drop)


## Current sole height in world space; the adapter reads this for diagnostics
## and for deciding whether the character is standing or floating.
func foot_height(side: String) -> float:
	if not _chains.has(side) or skeleton == null:
		return 0.0
	var foot: int = _chains[side].foot
	return (skeleton.global_transform * skeleton.get_bone_global_pose(foot).origin).y


func _process_modification() -> void:
	if skeleton == null or _chains.is_empty():
		return
	if _weight <= 0.0:
		return
	for side in _chains:
		var target: Dictionary = _targets.get(side, {})
		if not bool(target.get("active", false)):
			continue
		var side_weight := float(target.get("weight", 0.0)) * _weight
		if side_weight <= 0.0:
			continue
		_solve_side(_chains[side], float(target.get("y", 0.0)), side_weight)


func _solve_side(chain: Dictionary, goal_y: float, weight: float) -> void:
	var upper: int = chain.upper
	var lower: int = chain.lower
	var foot: int = chain.foot
	_clear_override(upper)
	_clear_override(lower)
	_clear_override(foot)
	var upper_pose := skeleton.get_bone_global_pose(upper)
	var lower_pose := skeleton.get_bone_global_pose(lower)
	var foot_pose := skeleton.get_bone_global_pose(foot)
	var space := skeleton.global_transform
	var end_world := space * foot_pose.origin
	if absf(end_world.y - goal_y) < 0.00001:
		return

	# Bone global poses are skeleton-local, not world-space. Solve in that same
	# space so a rotated/scaled character receives the same leg correction.
	var goal := space.affine_inverse() * Vector3(end_world.x, goal_y, end_world.z)
	var knee := lower_pose.origin - upper_pose.origin
	var result := Solver.solve(upper_pose.origin, lower_pose.origin, foot_pose.origin, goal, knee)
	if not result.has("mid"):
		return
	var delta_upper: Quaternion = Solver.rotation_between(knee, result.mid - upper_pose.origin)
	if weight < 0.999:
		delta_upper = Quaternion.IDENTITY.slerp(delta_upper, weight)
	var upper_basis := Basis(delta_upper) * upper_pose.basis.orthonormalized()
	skeleton.set_bone_global_pose_override(upper, Transform3D(upper_basis, upper_pose.origin), 1.0, true)

	lower_pose = skeleton.get_bone_global_pose(lower)
	var end_pose := skeleton.get_bone_global_pose(foot)
	var delta_lower: Quaternion = Solver.rotation_between(end_pose.origin - lower_pose.origin, result.end - lower_pose.origin)
	if weight < 0.999:
		delta_lower = Quaternion.IDENTITY.slerp(delta_lower, weight)
	var lower_basis := Basis(delta_lower) * lower_pose.basis.orthonormalized()
	skeleton.set_bone_global_pose_override(lower, Transform3D(lower_basis, lower_pose.origin), 1.0, true)
	# Moving the ankle must not rotate the shoe with the shin's correction.
	end_pose = skeleton.get_bone_global_pose(foot)
	skeleton.set_bone_global_pose_override(foot, Transform3D(foot_pose.basis, end_pose.origin), 1.0, true)


func _clear_override(bone: int) -> void:
	# Weight 0 with persistent=true releases a previously stored override.
	skeleton.set_bone_global_pose_override(bone, Transform3D(), 0.0, true)


## Releases every override this modifier could have written, so the character
## returns to its animated pose immediately instead of holding the last solve.
func reset_overrides() -> void:
	if skeleton == null:
		return
	for side in _chains:
		var chain: Dictionary = _chains[side]
		_clear_override(chain.upper)
		_clear_override(chain.lower)
		_clear_override(chain.foot)
	_targets.clear()
	for side in ["Left", "Right"]:
		if _chains.has(side):
			_targets[side] = {"active": false, "y": 0.0, "weight": 0.0}
