extends SceneTree

func _initialize() -> void:
	call_deferred("_run")

func _run() -> void:
	var view_script: Script = load("res://scripts/character_view.gd")
	var reports := []
	for id in ["godette", "swordsman"]:
		var view: Node3D = view_script.new()
		root.add_child(view)
		if not view.load_character({"config": "res://assets_data/%s.json" % id}):
			quit(1)
			return
		await process_frame
		await process_frame
		view.set_process(false)
		view.player.play("shared/idle", 0.0)
		view.player.advance(0)
		view.player.seek(7.0, true)
		view.skeleton.force_update_all_bone_transforms()
		reports.append(view.describe())
		view.free()
	var path := "D:/danjigame/3D-pet-gtpdo/docs/reports/stage-0/final-pose.json"
	var file := FileAccess.open(path, FileAccess.WRITE)
	file.store_string(JSON.stringify(reports, "\t"))
	print("FINAL_POSE ", JSON.stringify(reports))
	quit(0)
