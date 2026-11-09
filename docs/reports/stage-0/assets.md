# Stage 0 — humanoid assets and shared idle/wave

Date: 2026-11-01. Workspace: `D:\danjigame\3D-pet-gtpdo`.

Delivered one runtime model (Godette), two normalized motion sources and its runtime AnimationLibrary cache. The Swordsman/VRM1 sample entries below are historical notes recorded before those assets were removed from the repository. Asset tests ran in `tests/assets`; this agent did not launch/import the shared stage-0 project, use Git, spawn subagents, operate a visible application, or change the user's live Blender scene. The final desktop interaction matrix remains **pending**, as requested by the parent.

## Runtime interface — ready

Paths below are relative to `prototypes/stage0` / its `res://` root.

| Character | Model | Config | AnimationLibrary |
|---|---|---|---|
| Godette · VRM0 | `models/godette.vrm` | `assets_data/godette.json` | `motions/cache/godette.tres` |
| 剑士 · GLB | `models/swordsman.glb` | `assets_data/swordsman.json` | `motions/cache/swordsman.tres` |
| VRM1 local sample, disabled | Original absolute `D:/danjigame/3D-pet/work-pet/prototypes/azpepoze/models/AvatarSample_M.vrm` | `assets_data/avatar_sample_m.local.json` | Absolute `D:/danjigame/3D-pet-gtpdo/tests/assets/local_only/avatar_sample_m.tres` |

`assets_data/characters.json` is the requested two-entry array for Godette and Swordsman. It excludes VRM1.

Create an AnimationPlayer under the model instance, set `root_node = NodePath("..")`, load `config.animation_library`, attach it with `add_animation_library("shared", library)`, and play `shared/idle` or `shared/wave`. The cache track paths resolve from the model root. No source skeleton, runtime retarget modifier, or global-pose copying is required.

Preserve the imported target `Skeleton3D.motion_scale`. The final caches compensate for that value in hip position keys. Godette = `0.6456532478`, Swordsman = `1.0`, AvatarSample_M = `0.7576853633`. Setting it to 1 after loading a VRM would invalidate the compensated cache.

Each config contains the exact `skeleton_path`, profile-to-imported-name `bone_map`, `source_bone_map`, every bone's parent and local/global/world rest transforms, skeleton transform and motion scale, orientation, scale, mesh bindings and face expressions. `characters/*.json` mirrors the runtime configs. Display rotation is 180 degrees around Y for both imported VRMs and zero for Swordsman, presenting their front toward +Z. All measurements are in meters, +Y up. The parent's 1.6 m height normalization can use `display.rest_bounds_min/max` and scale `ground_offset_y` by the same factor.

Rest display bounds are computed from skin-weighted vertices at bone rest, not the unskinned mesh AABB. This matters for the Swordsman bind matrices: its final ground offset is `-0.2086735666` m and rest height `1.4149858057` m. Godette rest height is `1.4953148108` m. Swordsman's `hide_mesh_paths` contains the static `Cylinder_29/Object_41` display pedestal; honor that list. The model itself remains byte-for-byte original.

## Shared sources and retarget method

Only `Idle.fbx` and `Waving.fbx` were converted from the 62-file input folder. No seven-action or 62-action expansion was performed.

| Clip | Normalized source | Source frames | FPS | Length | Source bones |
|---|---|---:|---:|---:|---:|
| idle | `motions/idle.glb` | 1–251 inclusive | 30 | 8.333333 s | 65 |
| wave | `motions/wave.glb` | 1–17 inclusive | 30 | 0.533333 s | 65 |

The original names are `mixamorig1:Hips`, `mixamorig1:Spine`, etc. Godot sanitizes the colon to an underscore. Actual source tracks are `SourceRig/Skeleton3D:mixamorig1_Hips`, etc. Exact inspected source paths, track types, key counts and skeleton rests are in `animations/source_rig.json`; Blender import rest matrices, units, action ranges, source hashes and version are in `animations/source_conversion.json`.

The Blender FBX import yielded a 0.01-scale armature with an X-axis conversion rotation. The conversion script explicitly bakes those into joint translations, root rotation channels and inverse-bind matrices. Both delivered GLBs have identity SourceRig scale/rotation, meter bone translations, glTF +Y up and source +Z forward. They contain skeletons and one named clip each, without mesh payloads. The normalized hip rest height is approximately 1.00052 m.

Per-target caches are generated **only from those two normalized sources**:

1. Pair anatomical roles using an inspected map; use source upper-chest rotation for a target that collapses chest segments.
2. Align source and target forward directions (180-degree source-to-target Y rotation for VRMs).
3. Compute target rest-direction corrections for shoulders, arms, hands, legs, feet and applicable finger segments. This handles the Swordsman's lowered-arm bind pose, rather than treating it as a T-pose.
4. Transfer source world rotation deltas relative to source rest onto corrected target rest rotations, then resolve each local rotation from its actual target parent hierarchy.
5. Preserve every target non-hip local translation and every target scale. Transfer only hip displacement, scaled by mean target/source leg length. Divide serialized hip keys by the target's Godot motion scale.

This is a rest-aware, rotation-based bake. It never copies source global joint positions onto target joints. Missing optional humanoid segments remain in the target hierarchy with their own rest offsets. Godette uses 45 mapped rotation tracks + 1 hip position track, Swordsman 19 + 1, VRM1 52 + 1. Cache provenance and measured motion-scale factors are in `animations/cache_manifest.json`.

The 0.533-second wave is the full supplied source, not a fabricated long greeting. Both cache clips use linear looping. There is no authored transition, time extension, foot-lock IK, contact solver or root-motion locomotion extraction.

## Source provenance, hashes and attribution

Models and motions were read from the user's existing files. Model payloads were copied without edits; FBX files were never modified. `animations/asset_manifest.json` records hashes/sizes of delivered runtime assets and verifies source hashes again after conversion. SHA-256 source hashes:

| Source | SHA-256 |
|---|---|
| `pet动作模型/Idle.fbx` | `58f6cae7923e4e6fa4d43915f82820392c71625429c9c4a667a292d40a924504` |
| `pet动作模型/Waving.fbx` | `1aacd7344405311c165e8a4e9a15707acfe755749c323bc4025c5784cbfb8bb5` |
| `Godette_vrm_v4.vrm` | `0b008f60c808eb1c83781c650e654a70d512574932e3867f80609c62d7052c6c` |
| `角色/小纸人.glb` | `f8e18a42b410b8861c8595299b259c3550e6057ab7c6fbdd898cb3272fc28f7b` |
| external `AvatarSample_M.vrm` | `1fa11ee9d3ef98eec438df191f2278258d3fdd9f4aadc84a6ba6e10e2c467de1` |

Runtime source GLB hashes: idle `b5f0295c2e0cda3a07c750f8d6c8e8784f0fd7de0a9900abcabd736b197dc845`; wave `50a0498735468ff55882265b73178a9b327de67ccdf640894f94e10a88c783c7`.

Final runtime cache hashes: Godette `e9f0032d2e4d3f22e66b1931b0ca42b4bb91056ef6c739023f7d909c03dffa64`; Swordsman `42180e36651d92315812e3b9c82a1eab557880019790375a50895c6e0e68abb0`. Reload the resource/process when comparing against an earlier cache revision.

Actual embedded metadata:

- **Godette:** title “Low-Poly Godette”, version `0.21`, author `SirRichard94`, “Adapted to VRM by Lyuma”, license `CC_BY`. Source/contact: https://github.com/SirRichard94/low-poly-godette . Its embedded usage metadata allows everyone, commercial usage and the other enumerated usage flags. Preserve credit to SirRichard94 and Lyuma. The file name's “v4” is not its embedded metadata version.
- **Swordsman:** “LOW POLY character (swordsman)”, author IVA (`https://sketchfab.com/barsychokart12`), embedded `CC-BY-4.0`, source `https://sketchfab.com/3d-models/low-poly-character-swordsman-24aae01ca3a64a8c9dfc232840c2542b`.
- **VRM1:** author `pixiv VRoid Project`; embedded `avatarPermission=onlyAuthor`, `commercialUsage=personalNonProfit`, `creditNotation=required`, `allowRedistribution=false`, `modification=prohibited`, license URL `https://vrm.dev/licenses/1.0/`. **Local technical inspection only, not publishable.** No `.vrm`, converted `.glb`, or packed scene copy was added to the stage-0 project. Its cache and any posed mesh/image diagnostics are confined to `tests/assets/local_only`.
- **FBX motions:** user-supplied files with Mixamo-style bone names. Licensing provenance is **unverified**; a naming convention is not a redistribution grant. The model-level `publishable` flags do not establish clearance to publish the motion set.

`models/ATTRIBUTION.txt` carries model credit and the above motion/VRM1 boundary. Preserve it in any distributed package. Full embedded metadata for inspected candidates is in `characters/source_inventory.json`; several supplied GLBs were static meshes with no skin and were not selected.

## Facial expressions and secondary-motion metadata

| Target | Mesh path | Blink | Mouth A | Measured affected vertices |
|---|---|---|---|---|
| Godette | `Skeleton3D/Face` | indices 3,4: `morph_3`, `morph_4` | index 5: `morph_5` | 143 per eyelid; 40 mouth |
| VRM1 local | `Skeleton3D/Face` | index 13: `Fcl_EYE_Close` | index 39: `Fcl_MTH_A` | 981 blink; 1032 mouth |
| Swordsman | — | none | none | No blendshapes in supplied GLB |

Godette's raw glTF does not provide `targetNames`; the names above are actual Godot import names. Semantic mapping comes from VRM blendShapeGroups. The test checks both the imported names/indices and nonzero morph displacement, and writes/reads each weight independently. The caches contain no expression tracks, so the parent can drive these directly without an animation conflict.

Godette's actual spring roots from the VRM metadata and imported bone names are:

- `FrontHair_Center_L`, `FrontHair_Center_R`, `FrontHair_R`, `FrontHair_L` (stiffness 2.5, drag 0.4).
- `Ribbon` (stiffness 2, drag 0.4).
- `HairBall_L`, `HairBall_R`, `HairBall_Center` (stiffness 4, drag 0.1).

Their named `_End`/`_end` children are also listed in the config rest bones. All three groups have gravity power 0; two collider groups are present. These exact names can replace a generic case-sensitive “hair/skirt” probe. **Dynamic secondary motion has not been established by these asset tests:** modifiers were disabled to isolate cache playback. The parent reported `observed_secondary_rotation_radians=0`; that remains a runtime validation gap, not a pass.

## Verification and known limits

Tools: installed Blender **5.2.3 LTS** (background, factory startup), Godot **4.7.2 stable**, AzPepoze VRM addon **2.6.1** copied into the isolated test project. The inspected shared and isolated `import_vrm.gd` SHA-256 was `797478075b75fde463438c919951683858a0d95444f5e5cfefd2aca95423f72e`.

Evidence: `tests/assets/results/import_inspection.json`, `playback_verification.json`, `inspect.log`, `bake.log`, `verify.log`, and `verify_motion_scale_red.log`.

- Three Python asset contract tests pass: named meter-scale sources, runtime config/cache existence, and external-only restricted VRM1 policy.
- The engine loads the **saved** caches and drives AnimationPlayer with the same root/library convention as the parent. It checks all 251 idle and 17 wave samples for each of three targets: **804 sampled target poses**.
- Every track resolves to the intended bone, all scale/rest lengths remain intact, and non-hip offset error is exactly zero. Maximum bone-length discrepancy is below `0.000003` m (floating-point accumulation).
- Idle right hand stays below the shoulder. Wave right hand rises 0.0808 m (Godette), 0.1158 m (Swordsman), and 0.1105 m (VRM1) above the upper-arm origin. Each wave has actual hand travel, and the idle and wave poses differ.
- A meaningful regression check failed on the earlier VRM hip keys, then passed after Godot motion-scale compensation. Final maximum hip-height drift from rest is 0.00301 m Godette, 0.00473 m Swordsman, and 0.00439 m VRM1 over the clips.
- Skin-bind resolution, finite skinned vertices and morph data pass. CPU skinning follows Godot's skeleton-local `pose * bind` transform and the mesh instance transform; implementation reference: https://github.com/godotengine/godot/blob/master/scene/3d/skeleton_3d.cpp . This is a geometry check, not a GPU material/rendering acceptance test.
- `tests/assets/local_only/pose_review.png` is a background Blender clay render of sampled, CPU-skinned cache poses (all three targets), inspected for limb shape and raised-hand behavior. It deliberately remains local because it depicts the restricted VRM1 model. Its materials are diagnostic; it does not prove MToon appearance or desktop transparency.
- Parent integration now records **CHARACTER_TESTS12 passing** for both runtime models, independent blink/mouth behavior and visible upper-head hit detection. `secondary-tests.log` also records post-warm-up secondary rotation changes after moving the model. These are parent integration results, not additional shared-project launches by this asset agent.

Remaining boundaries:

1. **Floor contact is not accepted.** There is no foot-lock IK. Local sampled skinned Godette soles can be roughly 0.0165 m below the rest floor after arm/leg rest alignment. The earlier Release's foot-world Y near `-0.16` at 7 s came from the pre-compensation cache: the parent's final-cache check in `final-pose.json` now gives Godette hip Y `0.687808` and foot-joint Y `0.059133 / 0.065725`. This resolves that historical whole-body sinking observation; positive joint positions do not establish shoe-sole contact or replace visual acceptance.
2. Swordsman retains its source weapons/armor; waving raises its shield. Its source skeleton lacks a separate neck/chest and fingers. The bake preserves those limitations and uses the available 19 roles.
3. Short source wave looping and model-proportion differences can produce contact/transition artifacts; this delivery does not provide IK, transition animation or broader action coverage.
4. The copied importer's manual VRM1 lifecycle workaround duplicates `_import_post` on this installed Godot runtime, producing LookOffset errors and empty expression clips. `tools/assets/runtime_vrm_loader.gd` is the tested **single-pass external runtime loader**; it relies on engine callbacks and the existing addon extension classes. The shared addon was not patched. VRM1 local verification used this loader, not the problematic duplicate invocation.
5. Isolated VRM tests still emit **two ObjectDB leak warnings at shutdown** from the addon/import lifecycle. Verification exits 0 and no pose assertion fails, but clean addon teardown is not claimed.
6. The parent has since measured secondary-bone movement after warm-up, but secondary collision quality, final GPU material appearance, exported VRM1 loading, and the desktop click-through/focus/DPI/multi-monitor matrix remain unverified. No further app input was performed after the parent's UI automation stop; subsequent parent integration checks were headless.

## Rebuild and validation commands

Run from `D:\danjigame\3D-pet-gtpdo`. The isolated project already contains copies of `addons/vrm` and `addons/mtoon`; no main-project launch is needed. The loader expects those `res://addons` locations.

```powershell
$assetGodot = 'D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe'
$assetBlender = 'D:\steam\steamapps\common\Blender\blender.exe'
python tools/assets/inspect_sources.py
& $assetBlender --background --factory-startup --python-exit-code 1 --python tools/assets/convert_fbx.py
& $assetGodot --headless --path tests/assets --editor --import --quit
& $assetGodot --headless --path tests/assets --script res://inspect_imports.gd
python tools/assets/prepare_configs.py
& $assetGodot --headless --path tests/assets --script D:/danjigame/3D-pet-gtpdo/tools/assets/bake_libraries.gd
& $assetGodot --headless --path tests/assets --script res://verify_libraries.gd
python tests/assets/test_asset_contract.py
& $assetBlender --background --factory-startup --python-exit-code 1 --python tools/assets/render_pose_review.py
```

The source FBX/VRM/GLB paths are intentionally local and explicit in the tools/configs. Changing a model, imported bone layout, addon retarget settings, or `motion_scale` requires regenerating its cache and rerunning playback verification. This report covers the authorized asset scope only; no claim of full stage-0 desktop acceptance is made.
