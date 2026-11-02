"""Create exact runtime contracts from inspected engine skeletons and VRM metadata."""
import hashlib
import json
import pathlib
from inspect_sources import ROOT, OLD_MODELS, CHARACTERS, read_glb

STAGE = ROOT/'prototypes/stage0'
inspection = json.loads((ROOT/'tests/assets/results/import_inspection.json').read_text(encoding='utf-8'))
source_map = {'Hips':'Hips','Spine':'Spine','Chest':'Spine1','UpperChest':'Spine2','Neck':'Neck','Head':'Head'}
for side in ('Left','Right'):
    for profile,src in [('Shoulder','Shoulder'),('UpperArm','Arm'),('LowerArm','ForeArm'),('Hand','Hand'),('UpperLeg','UpLeg'),('LowerLeg','Leg'),('Foot','Foot'),('Toes','ToeBase')]:
        source_map[side+profile]=side+src
    for finger,mixamo in [('Thumb','Thumb'),('Index','Index'),('Middle','Middle'),('Ring','Ring'),('Little','Pinky')]:
        segments = ['Metacarpal','Proximal','Distal'] if finger=='Thumb' else ['Proximal','Intermediate','Distal']
        for i,segment in enumerate(segments,1): source_map[side+finger+segment] = side+'Hand'+mixamo+str(i)
source_map={k:'mixamorig1_'+v for k,v in source_map.items()}

sword_map={'Hips':'spine_26','Spine':'spine.003_13','Head':'spine.004_0'}
for side,suffix in [('Left','L'),('Right','R')]:
    names = ['shoulder.L_4','upper_arm.L_3','forearm.L_2','hand.L_1','thigh.L_20','shin.L_19','foot.L_18','toe.L_16'] if side=='Left' else ['shoulder.R_8','upper_arm.R_7','forearm.R_6','hand.R_5','thigh.R_25','shin.R_24','foot.R_23','toe.R_21']
    for key,name in zip(['Shoulder','UpperArm','LowerArm','Hand','UpperLeg','LowerLeg','Foot','Toes'],names): sword_map[side+key]=name

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def write(path,data): path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

for id in ('godette','swordsman','avatar_sample_m'):
    record=inspection[id]; sk=record['skeletons'][0]
    names={b['name'] for b in sk['bones']}
    bone_map=sword_map if id=='swordsman' else {k:k for k in source_map if k in names}
    mapping=dict(source_map)
    if id=='swordsman': mapping['Spine']='mixamorig1_Spine2'
    if id=='godette': mapping['Chest']='mixamorig1_Spine2'
    source=CHARACTERS/'小纸人.glb' if id=='swordsman' else OLD_MODELS/('Godette_vrm_v4.vrm' if id=='godette' else 'AvatarSample_M.vrm')
    doc=read_glb(source)
    meta=doc.get('extensions',{}).get('VRM',doc.get('extensions',{}).get('VRMC_vrm',{})).get('meta',doc['asset'].get('extras',{}))
    runtime_path={'godette':'res://models/godette.vrm','swordsman':'res://models/swordsman.glb','avatar_sample_m':source.as_posix()}[id]
    library=f'res://motions/cache/{id}.tres' if id!='avatar_sample_m' else (ROOT/'tests/assets/local_only/avatar_sample_m.tres').as_posix()
    expressions={}
    if id=='godette':
        expressions={'blink':[{'mesh_path':'Skeleton3D/Face','index':i,'name':f'morph_{i}','weight':1.0} for i in [3,4]],'mouth_a':[{'mesh_path':'Skeleton3D/Face','index':5,'name':'morph_5','weight':1.0}]}
    if id=='avatar_sample_m':
        shapes=next(m['blendshapes'] for m in record['meshes'] if m['path']=='Skeleton3D/Face')
        expressions={k:[{'mesh_path':'Skeleton3D/Face','index':shapes.index(n),'name':n,'weight':1.0}] for k,n in [('blink','Fcl_EYE_Close'),('mouth_a','Fcl_MTH_A')]}
    bounds_min=[min(m['skin_rest_position'][i] for m in record['meshes'] if not 'Cylinder' in m['path']) for i in range(3)]
    bounds_max=[max(m['skin_rest_position'][i]+m['skin_rest_size'][i] for m in record['meshes'] if not 'Cylinder' in m['path']) for i in range(3)]
    cfg={'schema_version':1,'id':id,'model_path':runtime_path,'format':'glb' if id=='swordsman' else ('vrm0' if id=='godette' else 'vrm1'),
         'animation_library':library,'animation_library_name':'shared','animations':['idle','wave'],'animation_root':'model_instance',
         'skeleton_path':sk['path'],'bone_map':bone_map,'source_bone_map':{k:mapping[k] for k in bone_map},
         'source_to_target_yaw_degrees':0 if id=='swordsman' else 180,
         'display':{'units':'meters','up':'+Y','model_forward':'+Z' if id=='swordsman' else '-Z','rotation_degrees':[0,0 if id=='swordsman' else 180,0],'scale':1.0,'ground_offset_y':-bounds_min[1],'rest_bounds_min':bounds_min,'rest_bounds_max':bounds_max},
         'rest':sk,'expressions':expressions,'inspected_meshes':record['meshes'],
         'source':{'path':str(source),'sha256':sha(source),'license_metadata':meta},
         'publishable':id!='avatar_sample_m','enabled_by_default':id!='avatar_sample_m',
         'import_options':{'vrm/bone_rename':1,'vrm/skeleton_name':'Skeleton3D','vrm/remove_end_bones':True,'vrm/v1_rotate_180':True} if id!='swordsman' else {},
         'retarget_method':'prebaked world rest rotation deltas with anatomical direction rest alignment; recomposed target local rotations; only hips translation; all other local offsets and scales retain target rest',
         'notes':['Animations sourced only from user Idle.fbx and Waving.fbx.','Publishability here describes the model only; FBX motion license provenance remains unverified.']}
    if id=='swordsman': cfg['hide_mesh_paths']=[m['path'] for m in record['meshes'] if 'Cylinder' in m['path']]
    if id=='avatar_sample_m': cfg['notes'].append('LOCAL TECHNICAL SAMPLE ONLY. Original metadata prohibits redistribution and modification. No model copy is included; keep this config disabled in export builds.')
    name=id+('.local' if id=='avatar_sample_m' else '')+'.json'
    write(STAGE/'assets_data'/name,cfg)
    write(ROOT/'characters'/name,cfg)
write(ROOT/'animations/source_rig.json',{'source_bone_map':source_map,'skeleton':inspection['idle']['skeletons'][0],'clips':{k:inspection[k]['players'][0] for k in ('idle','wave')}})
print('Wrote three character contracts and source rig metadata')
