extends RefCounted

var checks := 0
var failures := 0

func check(condition: bool, message: String) -> void:
	checks += 1
	if not condition:
		failures += 1
		push_error(message)

func run() -> int:
	var guard: Script = load("res://verifier.gd")
	if not guard.has_method("packaged_path_exists"):
		check(false, "package verifier must expose the resource-aware exclusion guard")
	else:
		var paths := ["res://private.json", "res://forbidden.gd", "res://pixel.svg"]
		# These assertions prove the export actually exercised compilation/import.
		check(not FileAccess.file_exists(paths[1]) and FileAccess.file_exists(paths[1] + ".remap"), "fixture script must be compiled and remapped in the export")
		check(not FileAccess.file_exists(paths[2]) and FileAccess.file_exists(paths[2] + ".import"), "fixture image must be imported in the export")
		check(ResourceLoader.exists(paths[1]) and ResourceLoader.exists(paths[2]), "compiled and imported fixture resources are genuinely loadable")
		for path in paths:
			check(guard.packaged_path_exists(path), "guard must reject included resource " + path)
		check(not guard.packaged_path_exists("res://missing.gd"), "guard allows an absent script")
		check(not guard.packaged_path_exists("res://missing.svg"), "guard allows an absent imported resource")
		check(not guard.packaged_path_exists("res://missing.json"), "guard allows absent raw metadata")
		if "--reject-fixtures" in OS.get_cmdline_user_args():
			var rejected := 0
			for path in paths:
				if guard.packaged_path_exists(path):
					rejected += 1
					print("REJECTED ", path)
			print("NEGATIVE_EXPORT rejected=", rejected, " checks_failed=", failures)
			return 1 if rejected == 3 and failures == 0 else 2
	print("PACKAGE_GUARD_TEST checks=", checks, " failures=", failures)
	return 0 if failures == 0 else 1
