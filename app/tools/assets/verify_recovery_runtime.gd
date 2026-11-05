# Forwarding stub: the canonical script lives outside the Godot project at
# tools/assets/verify_recovery_runtime.gd (driven by tools/build_stage2.py via
# an absolute --script path). The merge test harness runs it as
# res://tools/assets/verify_recovery_runtime.gd with --path app, so re-expose
# it here without keeping a second editable copy of the runtime checks.
extends "res://../tools/assets/verify_recovery_runtime.gd"
