"""Run with factory-startup background Blender; only Idle and Waving are read."""
import bpy
import json
import pathlib
import hashlib
import struct
from mathutils import Matrix, Quaternion, Vector


def normalize_skeleton_glb(path, clip):
    """Bake the FBX centimeter armature transform into meter/Y-up bone data."""
    blob = path.read_bytes()
    size = struct.unpack_from('<I', blob, 12)[0]
    doc = json.loads(blob[20:20+size])
    binary = bytearray(blob[28+size:])
    rig = next(n for n in doc['nodes'] if n.get('name') == 'SourceRig')
    unit = rig['scale'][0]
    assert abs(unit-.01) < 1e-6 and len(rig['children']) == 1
    root = rig['children'][0]
    xyzw = rig['rotation']
    rotation = Quaternion((xyzw[3], *xyzw[:3]))

    def accessor_values(index):
        a = doc['accessors'][index]; v = doc['bufferViews'][a['bufferView']]
        assert a['componentType'] == 5126
        width = {'VEC3':3,'VEC4':4,'MAT4':16}[a['type']]
        offset = v.get('byteOffset',0) + a.get('byteOffset',0)
        stride = v.get('byteStride',4*width)
        return a, [(offset+i*stride, struct.unpack_from('<'+'f'*width,binary,offset+i*stride)) for i in range(a['count'])]

    for i,n in enumerate(doc['nodes']):
        if 'translation' in n:
            p = Vector(n['translation'])*unit
            n['translation'] = list(rotation@p if i == root else p)
        if i == root:
            q = n.get('rotation',[0,0,0,1]); q = rotation@Quaternion((q[3],*q[:3]))
            n['rotation'] = [q.x,q.y,q.z,q.w]
    rig['rotation'] = [0,0,0,1]; rig['scale'] = [1,1,1]
    touched = set()
    for a in doc.get('animations',[]):
        a['name'] = clip
        for ch in a['channels']:
            index = a['samplers'][ch['sampler']]['output']
            target = ch['target']; kind = target['path']
            if index in touched: continue
            if kind == 'translation' or (kind == 'rotation' and target['node'] == root):
                touched.add(index)
                accessor, values = accessor_values(index)
                out = []
                for offset,v in values:
                    if kind == 'translation':
                        p = Vector(v)*unit; v = list(rotation@p if target['node'] == root else p)
                    else:
                        q = rotation@Quaternion((v[3],*v[:3])); v=[q.x,q.y,q.z,q.w]
                    struct.pack_into('<'+'f'*len(v),binary,offset,*v); out.append(v)
                if 'min' in accessor: accessor['min']=[min(v[k] for v in out) for k in range(len(out[0]))]
                if 'max' in accessor: accessor['max']=[max(v[k] for v in out) for k in range(len(out[0]))]
    parents = {c:i for i,n in enumerate(doc['nodes']) for c in n.get('children',[])}
    globals_ = {}
    def world(i):
        if i not in globals_:
            n=doc['nodes'][i]; q=n.get('rotation',[0,0,0,1])
            m=Matrix.LocRotScale(Vector(n.get('translation',[0,0,0])),Quaternion((q[3],*q[:3])),Vector(n.get('scale',[1,1,1])))
            globals_[i] = world(parents[i])@m if i in parents else m
        return globals_[i]
    for skin in doc['skins']:
        _,values=accessor_values(skin['inverseBindMatrices'])
        for joint,(offset,_) in zip(skin['joints'],values):
            inv=world(joint).inverted()
            struct.pack_into('<16f',binary,offset,*[inv[r][c] for c in range(4) for r in range(4)])
    doc['asset']['extras']={'units':'meters','up':'+Y','forward':'+Z','source_clip':clip,'normalization':'FBX object centimeter scale and X90 rotation baked into joints; bone names preserved'}
    js=json.dumps(doc,separators=(',',':')).encode(); js+=b' '*((-len(js))%4)
    binary+=b'\0'*((-len(binary))%4)
    path.write_bytes(struct.pack('<4sII',b'glTF',2,28+len(js)+len(binary))+struct.pack('<I4s',len(js),b'JSON')+js+struct.pack('<I4s',len(binary),b'BIN\0')+binary)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = pathlib.Path(r'D:\danjigame\模型all\pet动作模型')
records = []
for source_name, clip in [('Idle.fbx', 'idle'), ('Waving.fbx', 'wave')]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source = SOURCE/source_name
    bpy.ops.import_scene.fbx(filepath=str(source), use_anim=True, automatic_bone_orientation=False)
    armatures = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    assert len(armatures) == 1
    arm = armatures[0]
    action = arm.animation_data.action
    action.name = clip
    start, end = action.frame_range
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = int(start), int(end)
    # FBX importer uses the file's time mode. Preserve this sampling rate.
    fps = scene.render.fps / scene.render.fps_base
    scene.frame_set(int(start))
    rec = {'clip':clip,'source':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
           'blender_version':bpy.app.version_string,'fps':fps,'frame_start':start,'frame_end':end,
           'armature':arm.name,'armature_matrix':[list(r) for r in arm.matrix_world],
           'bones':[{'name':b.name,'parent':b.parent.name if b.parent else None,
                     'head_world':list(arm.matrix_world @ b.head_local),
                     'tail_world':list(arm.matrix_world @ b.tail_local),
                     'rest_matrix':[list(r) for r in b.matrix_local]} for b in arm.data.bones]}
    # Skeleton-only GLBs retain original FBX bone names and motion. Blender glTF
    # export converts Blender Z-up to glTF Y-up and bakes armature object scale.
    for obj in list(bpy.data.objects):
        if obj.type != 'ARMATURE':
            bpy.data.objects.remove(obj, do_unlink=True)
    arm.name = 'SourceRig'
    bpy.ops.export_scene.gltf(filepath=str(ROOT/f'prototypes/stage0/motions/{clip}.glb'),
        export_format='GLB', export_yup=True, export_animations=True,
        export_animation_mode='ACTIVE_ACTIONS', export_frame_range=True,
        export_force_sampling=True, export_anim_slide_to_zero=True,
        export_skins=True, export_def_bones=False)
    normalize_skeleton_glb(ROOT/f'prototypes/stage0/motions/{clip}.glb', clip)
    records.append(rec)
(ROOT/'animations/source_conversion.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
print('PET3D_CONVERTED', [(r['clip'],len(r['bones']),r['fps'],r['frame_end']) for r in records])
