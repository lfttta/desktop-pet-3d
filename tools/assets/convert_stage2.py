"""Convert the seven Stage 2 FBX candidates into normalized skeleton-only GLBs.

Paths are reproducible on any machine: --repo-root/--fbx-source (passed to this
script after Blender's ``--``) override PET3D_REPO_ROOT/PET3D_FBX_SOURCE, which
override the default project paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys

import bpy
from mathutils import Matrix, Quaternion, Vector


DEFAULT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_ROOT = Path(r"D:\danjigame\模型all\pet动作模型")


def resolve_paths(argv: list[str], environ: dict) -> tuple[Path, Path]:
    """CLI wins over the environment override, which wins over the defaults.

    Blender keeps its own options in sys.argv and passes the script's options
    after a ``--`` separator. ``argparse`` treats everything after ``--`` as
    positional, so the separator is split off here first; without it the whole
    argv is parsed.
    """
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--fbx-source", type=Path, default=None)
    args, _ = parser.parse_known_args(argv)
    root = args.repo_root
    if root is None:
        value = environ.get("PET3D_REPO_ROOT", "")
        root = Path(value) if value else DEFAULT_ROOT
    source = args.fbx_source
    if source is None:
        value = environ.get("PET3D_FBX_SOURCE", "")
        source = Path(value) if value else DEFAULT_SOURCE_ROOT
    return root, source


ROOT, SOURCE_ROOT = resolve_paths(sys.argv[1:], os.environ)
OUTPUT = ROOT / "build/stage2/motions"


def accessor_values(doc, binary, index):
    accessor = doc["accessors"][index]
    view = doc["bufferViews"][accessor["bufferView"]]
    if accessor["componentType"] != 5126:
        raise ValueError("Only float animation accessors are supported")
    width = {"VEC3": 3, "VEC4": 4, "MAT4": 16}[accessor["type"]]
    offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride", 4 * width)
    values = [(offset + i * stride, struct.unpack_from("<" + "f" * width, binary, offset + i * stride)) for i in range(accessor["count"])]
    return accessor, values


def normalize_glb(path: Path, clip: str, root_motion: str) -> None:
    blob = path.read_bytes()
    json_size = struct.unpack_from("<I", blob, 12)[0]
    doc = json.loads(blob[20:20 + json_size])
    binary = bytearray(blob[28 + json_size:])
    rig = next(node for node in doc["nodes"] if node.get("name") == "SourceRig")
    unit = rig.get("scale", [1.0])[0]
    if abs(unit - 0.01) > 1e-5 or len(rig["children"]) != 1:
        raise ValueError(f"{clip}: unexpected FBX armature transform")
    root = rig["children"][0]
    xyzw = rig.get("rotation", [0, 0, 0, 1])
    rotation = Quaternion((xyzw[3], *xyzw[:3]))

    for index, node in enumerate(doc["nodes"]):
        if "translation" in node:
            position = Vector(node["translation"]) * unit
            node["translation"] = list(rotation @ position if index == root else position)
        if index == root:
            value = node.get("rotation", [0, 0, 0, 1])
            value = rotation @ Quaternion((value[3], *value[:3]))
            node["rotation"] = [value.x, value.y, value.z, value.w]
    rig["rotation"] = [0, 0, 0, 1]
    rig["scale"] = [1, 1, 1]

    touched = set()
    for animation in doc.get("animations", []):
        animation["name"] = clip
        for channel in animation["channels"]:
            sampler = animation["samplers"][channel["sampler"]]
            index = sampler["output"]
            target = channel["target"]
            kind = target["path"]
            if index in touched or (kind not in {"translation", "rotation"}):
                continue
            if kind == "rotation" and target["node"] != root:
                continue
            touched.add(index)
            accessor, values = accessor_values(doc, binary, index)
            transformed = []
            for offset, raw in values:
                if kind == "translation":
                    value = Vector(raw) * unit
                    if target["node"] == root:
                        value = rotation @ value
                    transformed.append([value.x, value.y, value.z])
                else:
                    value = rotation @ Quaternion((raw[3], *raw[:3]))
                    transformed.append([value.x, value.y, value.z, value.w])
            if kind == "translation" and target["node"] == root and transformed:
                origin = transformed[0]
                for value in transformed:
                    if root_motion in {"none", "vertical_only"}:
                        value[0] = origin[0]
                        value[2] = origin[2]
                    if root_motion == "none":
                        value[1] = origin[1]
            for (offset, _), value in zip(values, transformed):
                struct.pack_into("<" + "f" * len(value), binary, offset, *value)
            if transformed:
                accessor["min"] = [min(value[k] for value in transformed) for k in range(len(transformed[0]))]
                accessor["max"] = [max(value[k] for value in transformed) for k in range(len(transformed[0]))]

    parents = {child: parent for parent, node in enumerate(doc["nodes"]) for child in node.get("children", [])}
    globals_ = {}

    def world(index):
        if index not in globals_:
            node = doc["nodes"][index]
            value = node.get("rotation", [0, 0, 0, 1])
            local = Matrix.LocRotScale(Vector(node.get("translation", [0, 0, 0])), Quaternion((value[3], *value[:3])), Vector(node.get("scale", [1, 1, 1])))
            globals_[index] = world(parents[index]) @ local if index in parents else local
        return globals_[index]

    for skin in doc["skins"]:
        _, values = accessor_values(doc, binary, skin["inverseBindMatrices"])
        for joint, (offset, _) in zip(skin["joints"], values):
            inverse = world(joint).inverted()
            struct.pack_into("<16f", binary, offset, *[inverse[row][column] for column in range(4) for row in range(4)])
    doc["asset"]["extras"] = {"units": "meters", "up": "+Y", "forward": "+Z", "source_clip": clip, "root_motion": root_motion}
    encoded = json.dumps(doc, separators=(",", ":")).encode()
    encoded += b" " * ((-len(encoded)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 28 + len(encoded) + len(binary)) + struct.pack("<I4s", len(encoded), b"JSON") + encoded + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def convert(clip: str, source: Path, root_motion: str) -> dict:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=str(source), use_anim=True)
    armatures = [obj for obj in bpy.data.objects if obj.type == "ARMATURE"]
    if len(armatures) != 1 or armatures[0].animation_data is None or armatures[0].animation_data.action is None:
        raise ValueError(f"{source.name}: expected one animated armature")
    armature = armatures[0]
    action = armature.animation_data.action
    action.name = clip
    start, end = action.frame_range
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = int(start), int(end)
    scene.frame_set(int(start))
    fps = scene.render.fps / scene.render.fps_base
    for obj in list(bpy.data.objects):
        if obj.type != "ARMATURE":
            bpy.data.objects.remove(obj, do_unlink=True)
    armature.name = "SourceRig"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{clip}.glb"
    bpy.ops.export_scene.gltf(filepath=str(destination), export_format="GLB", export_yup=True, export_animations=True, export_animation_mode="ACTIVE_ACTIONS", export_frame_range=True, export_force_sampling=True, export_anim_slide_to_zero=True, export_skins=True, export_def_bones=False)
    normalize_glb(destination, clip, root_motion)
    return {"source_file": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "semantic_status": "verified" if clip in {"idle", "walk", "wave", "fall"} else "candidate", "fps": fps, "frame_start": start, "frame_end": end, "output": destination.relative_to(ROOT).as_posix()}


def author_action(clip: str, rule: dict) -> dict:
    """Author short semantic actions once on the common source rig."""
    source = SOURCE_ROOT / rule["source"]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=str(source), use_anim=True)
    armature = next(obj for obj in bpy.data.objects if obj.type == "ARMATURE")
    scene = bpy.context.scene
    scene.frame_set(1)
    baseline = {bone.name: bone.matrix_basis.copy() for bone in armature.pose.bones}
    hips = armature.pose.bones["mixamorig1:Hips"]
    head = armature.pose.bones["mixamorig1:Head"]
    up = (head.head - hips.head).normalized()
    left = (armature.pose.bones["mixamorig1:LeftUpLeg"].head - armature.pose.bones["mixamorig1:RightUpLeg"].head).normalized()
    height = hips.head.dot(up)
    foot_positions = {side: armature.pose.bones[f"mixamorig1:{side}Foot"].head.copy() for side in ("Left", "Right")}
    armature.animation_data_clear()
    action = bpy.data.actions.new(clip)
    armature.animation_data_create().action = action
    armature.name = "SourceRig"
    for obj in list(bpy.data.objects):
        if obj != armature:
            bpy.data.objects.remove(obj, do_unlink=True)
    # Blender's IK solver plants both feet while the pelvis absorbs landing.
    if clip == "recover":
        for side in ("Left", "Right"):
            target = bpy.data.objects.new(f"{side}LandingTarget", None)
            scene.collection.objects.link(target)
            target.location = armature.matrix_world @ foot_positions[side]
            constraint = armature.pose.bones[f"mixamorig1:{side}Leg"].constraints.new("IK")
            constraint.target = target
            constraint.chain_count = 2
            constraint.use_stretch = False
    duration = {"turn": 0.8, "pickup": 0.4, "recover": 0.7}[clip]
    count = round(duration * 30)
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, count + 1

    def rotate(name: str, axis: Vector, degrees: float) -> None:
        bone = armature.pose.bones["mixamorig1:" + name]
        matrix = bone.matrix.copy()
        rotation = Quaternion(axis, math.radians(degrees)).to_matrix().to_4x4()
        bone.matrix = Matrix.Translation(matrix.translation) @ rotation @ Matrix.Translation(-matrix.translation) @ matrix
        bpy.context.view_layer.update()

    for index in range(count + 1):
        scene.frame_set(index + 1)
        for bone in armature.pose.bones:
            bone.rotation_mode = "QUATERNION"
            bone.matrix_basis = baseline[bone.name].copy()
        bpy.context.view_layer.update()
        t = index / count
        if clip == "recover":
            depth = math.sin(math.pi * t) * height * 0.1
            matrix = hips.matrix.copy()
            matrix.translation -= up * depth
            hips.matrix = matrix
            bpy.context.view_layer.update()
        else:
            blend = t * t * (3 - 2 * t)
            for side, sign in (("Left", 1), ("Right", -1)):
                amount = blend if clip == "pickup" else max(0.0, math.sin(t * 2 * math.pi) * sign)
                rotate(side + "UpLeg", left, -6.0 * amount)
                rotate(side + "Leg", left, 12.0 * amount)
                if clip == "pickup":
                    rotate(side + "Foot", left, -12.0 * amount)
                    rotate(side + "Arm", left.cross(up).normalized(), sign * 18.0 * amount)
        for bone in armature.pose.bones:
            bone.keyframe_insert("location", frame=index + 1)
            bone.keyframe_insert("rotation_quaternion", frame=index + 1)
            bone.keyframe_insert("scale", frame=index + 1)
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    destination = OUTPUT / f"{clip}.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(destination), export_format="GLB", use_selection=True,
        export_yup=True, export_animations=True, export_animation_mode="ACTIVE_ACTIONS",
        export_frame_range=True, export_force_sampling=True, export_anim_slide_to_zero=True,
        export_skins=True, export_def_bones=False,
    )
    normalize_glb(destination, clip, rule["root_motion"])
    return {"source_file": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "semantic_status": "authored", "derivation": rule["derived"], "fps": 30,
            "frame_start": 1, "frame_end": count + 1, "output": destination.relative_to(ROOT).as_posix()}


def glb_extras(path: Path) -> dict:
    blob = path.read_bytes()
    size = struct.unpack_from("<I", blob, 12)[0]
    return json.loads(blob[20:20 + size]).get("asset", {}).get("extras", {})


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached_record(clip: str, rule: dict, previous: dict, staged: Path, runtime: Path) -> dict | None:
    """Reuse a cached conversion only when its provenance still matches the
    manifest entry: same source file and source bytes, same derivation, the
    root-motion policy read back from the GLB extras, and a byte-identical
    runtime copy. Anything else is converted again."""
    record = previous.get(clip)
    if not staged.is_file() or not runtime.is_file() or record is None:
        return None
    if record.get("source_file") != rule["source"] or record.get("derivation") != rule.get("derived"):
        return None
    source = SOURCE_ROOT / rule["source"]
    if not source.is_file() or record.get("source_sha256") != sha256(source):
        return None
    if glb_extras(staged).get("root_motion") != rule["root_motion"]:
        return None
    staged_digest = sha256(staged)
    if sha256(runtime) != staged_digest:
        return None
    if record.get("output_sha256", staged_digest) != staged_digest:
        return None
    record["output_sha256"] = staged_digest
    return record


def main() -> None:
    manifest = json.loads((ROOT / "animations/humanoid_core/manifest.json").read_text("utf-8"))
    runtime_source = ROOT / "app/resources/animations/source"
    path = ROOT / "animations/humanoid_core/source_manifest.json"
    # Incremental: a clip may only be kept when cached_record proves that both
    # the manifest entry and the source bytes are unchanged; the record also
    # carries the output digest so a hand-edited GLB is detected next run.
    previous = json.loads(path.read_text("utf-8")).get("clips", {}) if path.is_file() else {}
    clips = {}
    converted: list[str] = []
    for clip, rule in manifest["actions"].items():
        record = cached_record(clip, rule, previous, OUTPUT / f"{clip}.glb", runtime_source / f"{clip}.glb")
        if record is not None:
            clips[clip] = record
            continue
        clips[clip] = author_action(clip, rule) if "derived" in rule else convert(clip, SOURCE_ROOT / rule["source"], rule["root_motion"])
        clips[clip]["output_sha256"] = sha256(OUTPUT / f"{clip}.glb")
        converted.append(clip)
    runtime_source.mkdir(parents=True, exist_ok=True)
    for clip in converted:
        shutil.copy2(OUTPUT / f"{clip}.glb", runtime_source / f"{clip}.glb")
    # Explicit LF: .gitattributes normalizes text to LF, so a CRLF write would
    # show every line as changed on the next checkout.
    path.write_text(json.dumps({"schema_version": 1, "clips": clips}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("PET3D_STAGE2_CONVERTED", {name: value.get("frame_end") for name, value in clips.items()})


if __name__ == "__main__":
    main()
