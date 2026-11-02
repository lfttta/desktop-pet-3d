extends SceneTree

const IDS := ["godette", "alicia", "soldier"]
const CharacterView = preload("res://scripts/character_view.gd")

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
	var baseline_nodes := int(Performance.get_monitor(Performance.OBJECT_NODE_COUNT))
	var memory_after_warmup := 0.0
	var peak_nodes := baseline_nodes
	for iteration in range(30):
		var character_id: String = IDS[iteration % IDS.size()]
		var view := CharacterView.new()
		root.add_child(view)
		await process_frame
		check(view.load_character({"config": "res://assets_data/%s.json" % character_id}), "switch %d loads %s" % [iteration + 1, character_id])
		check(view.request_action("wave", "user") > 0, "switch %d accepts an action" % [iteration + 1])
		peak_nodes = maxi(peak_nodes, int(Performance.get_monitor(Performance.OBJECT_NODE_COUNT)))
		view.free()
		await process_frame
		await process_frame
		check(int(Performance.get_monitor(Performance.OBJECT_NODE_COUNT)) <= baseline_nodes + 2, "switch %d releases scene nodes" % [iteration + 1])
		if iteration == 9:
			memory_after_warmup = Performance.get_monitor(Performance.MEMORY_STATIC)
	var final_memory := Performance.get_monitor(Performance.MEMORY_STATIC)
	var growth_after_warmup := maxf(0.0, final_memory - memory_after_warmup)
	check(growth_after_warmup < 32.0 * 1024.0 * 1024.0, "30 switches keep post-warmup memory growth below 32 MiB")
	print("STAGE2_SWITCH_TESTS checks=", checks, " failures=", failures, " peak_nodes=", peak_nodes, " growth_bytes=", int(growth_after_warmup))
	quit(0 if failures == 0 else 1)
