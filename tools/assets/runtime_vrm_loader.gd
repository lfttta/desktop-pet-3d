extends RefCounted
## Local external VRM loader for Godot 4.7.2. No file writes or packed model copy.
## Paths refer to the host project's existing AzPepoze VRM addon.

static func load_vrm(path: String) -> Node3D:
	var classes := [
		"res://addons/vrm/importer/v0/vrm_extension.gd",
		"res://addons/vrm/importer/v1/vrmc/vrmc_node_constraint.gd",
		"res://addons/vrm/importer/v1/vrmc/vrmc_spring_bone.gd",
		"res://addons/vrm/importer/v1/vrmc/vrmc_materials_mtoon.gd",
		"res://addons/vrm/importer/v1/vrmc/vrmc_materials_hdr_emissive_multiplier.gd",
		"res://addons/vrm/importer/v1/vrmc/vrmc_vrm.gd",
	]
	var extensions := []
	for script_path in classes:
		var extension = load(script_path).new()
		extensions.append(extension)
		GLTFDocument.register_gltf_document_extension(extension, true)
	var state := GLTFState.new()
	state.handle_binary_image = GLTFState.HANDLE_BINARY_EMBED_AS_UNCOMPRESSED
	state.set_meta("vrm_bone_rename", 1)
	state.set_meta("vrm_skeleton_name", "Skeleton3D")
	state.set_meta("vrm_remove_end_bones", true)
	state.set_meta("vrm_v1_rotate_180", true)
	state.set_additional_data("vrm/remove_end_bones", true)
	state.set_additional_data("vrm/v1_rotate_180", true)
	state.set_additional_data("vrm/head_hiding_method", 0)
	var doc := GLTFDocument.new()
	var err := doc.append_from_file(path, state, 8)
	var model: Node3D = doc.generate_scene(state) if err == OK else null
	for extension in extensions:
		GLTFDocument.unregister_gltf_document_extension(extension)
	return model
