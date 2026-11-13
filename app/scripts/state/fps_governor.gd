extends RefCounted
## Roadmap 3-F: tiered frame pacing, as pure logic so the tiers and the
## hysteresis are testable without a display server.
##
## The base tiers read the window's configurable caps (settings.performance:
## idle_fps/drag_fps/hidden_fps): drag, any action/walk/physics and visible
## idle each run at the applied drag or idle cap, and hidden runs at the
## effective hidden cap. On that base sit the local overlay tiers: an
## occluding fullscreen/locked foreground and long idle.
##
## Upgrades apply immediately so interaction never waits; downgrades wait out
## DOWNGRADE_HYSTERESIS so a flickering input cannot flap the frame rate.

## Base tier values. The window syncs these from the applied settings every
## frame; the defaults match service/settings.py.
var drag_fps := 60
var idle_fps := 30
var hidden_fps := 5
const FPS_LONG_IDLE := 10
const FPS_OCCLUDED := 5
## Seconds of app-level quiet before the long-idle tier kicks in.
const LONG_IDLE_SECONDS := 120.0
const DOWNGRADE_HYSTERESIS := 0.8
## Service desktop states that mean the pet is covered by another window.
const OCCLUDED_STATES := ["fullscreen", "locked"]

## Starts at the default idle cap; the first update() corrects it.
var current := 30
var _pending := 30
var _pending_elapsed := 0.0

func target_fps(hidden: bool, dragging: bool, active: bool, idle_seconds: float, desktop_state: String) -> int:
	if hidden:
		return hidden_fps
	if dragging:
		return drag_fps
	if active:
		return idle_fps
	if desktop_state in OCCLUDED_STATES:
		return FPS_OCCLUDED
	if idle_seconds >= LONG_IDLE_SECONDS or desktop_state == "idle":
		return FPS_LONG_IDLE
	return idle_fps

func update(delta: float, hidden: bool, dragging: bool, active: bool, idle_seconds: float, desktop_state: String) -> int:
	var target := target_fps(hidden, dragging, active, idle_seconds, desktop_state)
	# Hiding drops immediately (the contents are gone); any upgrade is instant
	# so a walk or a grab never starts at the idle frame rate.
	if hidden or target >= current:
		current = target
		_pending = target
		_pending_elapsed = 0.0
		return current
	if target != _pending:
		_pending = target
		_pending_elapsed = 0.0
	_pending_elapsed += maxf(0.0, delta)
	if _pending_elapsed >= DOWNGRADE_HYSTERESIS:
		current = target
		_pending_elapsed = 0.0
	return current
