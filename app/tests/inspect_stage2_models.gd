extends SceneTree


func _initialize() -> void:
    for character_id in ["cartoon_skeleton", "man"]:
        var scene := load("res://models/" + character_id + ".glb") as PackedScene
        if scene == null:
            push_error("Missing imported model: " + character_id)
            quit(1)
            return
        var root := scene.instantiate()
        get_root().add_child(root)
        var skeletons := root.find_children("*", "Skeleton3D", true, false)
        if skeletons.size() != 1:
            push_error(character_id + " expected one skeleton")
            quit(2)
            return
        var skeleton := skeletons[0] as Skeleton3D
        var names: Array[String] = []
        for index in skeleton.get_bone_count():
            names.append(skeleton.get_bone_name(index))
        print("STAGE2_MODEL ", character_id, " path=", root.get_path_to(skeleton), " bones=", skeleton.get_bone_count(), " names=", JSON.stringify(names))
        root.queue_free()
    await process_frame
    quit(0)
