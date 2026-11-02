"""Background Blender clay review from Godot CPU-skinned, sampled cache poses.

The restricted VRM1 render remains under tests/assets/local_only.
This does not open or save a .blend file and does not touch a live Blender.
"""
import bpy
import math
import pathlib
from mathutils import Vector

ROOT=pathlib.Path(__file__).resolve().parents[2]
bpy.ops.wm.read_factory_settings(use_empty=True)
scene=bpy.context.scene
scene.render.engine='CYCLES'
scene.cycles.samples=24
scene.render.resolution_x=1350
scene.render.resolution_y=1000
scene.render.resolution_percentage=100
scene.world=bpy.data.worlds.new('Review World')
scene.world.color=(0.16,0.16,0.16)
scene.view_settings.view_transform='Standard'
colors=[(0.22,0.55,0.85,1),(0.77,0.43,0.23,1),(0.38,0.65,0.42,1)]

def material(name,color):
    m=bpy.data.materials.new(name);m.diffuse_color=color
    m.use_nodes=True
    bsdf=next(n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
    bsdf.inputs['Base Color'].default_value=color
    bsdf.inputs['Roughness'].default_value=.8
    return m

text_material=material('Labels',(0.08,0.10,0.14,1))
def label(text,x,z,size=.13):
    curve=bpy.data.curves.new('Label','FONT');curve.body=text;curve.align_x='CENTER';curve.size=size
    obj=bpy.data.objects.new(text,curve);scene.collection.objects.link(obj)
    obj.location=(x,-.4,z);obj.rotation_euler=(math.pi/2,0,0);obj.data.materials.append(text_material)

for col,id in enumerate(['godette','swordsman','avatar_sample_m']):
    mat=material(id,colors[col])
    x=col*1.85
    for row,clip in enumerate(['idle','wave']):
        folder='local_only' if id=='avatar_sample_m' else 'results'
        path=ROOT/f'tests/assets/{folder}/{id}_{clip}.obj'
        vs=[];fs=[]
        for line in path.read_text().splitlines():
            a=line.split()
            if a[0]=='v':
                xx,yy,zz=map(float,a[1:]);vs.append((xx,-zz,yy))
            if a[0]=='f': fs.append(tuple(int(i)-1 for i in a[1:]))
        mesh=bpy.data.meshes.new(id+'_'+clip);mesh.from_pydata(vs,[],fs);mesh.update()
        obj=bpy.data.objects.new(id+'_'+clip,mesh);scene.collection.objects.link(obj)
        obj.location=(x,0,(1-row)*1.95)
        obj.data.materials.append(mat)
        for poly in obj.data.polygons:poly.use_smooth=True
        label(clip.upper(),x,(1-row)*1.95-.16)
    label({'godette':'Godette / VRM0','swordsman':'Swordsman / GLB','avatar_sample_m':'AvatarSample_M / VRM1'}[id],x,3.65)
label('Same FBX clips | CPU-skinned cached poses | clay materials',1.85,3.96,.14)
label('LOCAL TECHNICAL REVIEW - VRM1 NOT FOR PUBLICATION',1.85,-.50,.12)
bpy.ops.mesh.primitive_plane_add(size=200,location=(0,1,0),rotation=(math.pi/2,0,0))
bpy.context.object.data.materials.append(material('Background',(.84,.87,.91,1)))
for loc,power,size in [((0,-4,6),160,5),((5,-3,4),100,4)]:
    bpy.ops.object.light_add(type='AREA',location=loc)
    lamp=bpy.context.object;lamp.data.energy=power;lamp.data.shape='DISK';lamp.data.size=size
    lamp.rotation_euler=(Vector((1.85,0,2))-lamp.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.object.camera_add(location=(1.85,-10,1.8))
cam=bpy.context.object;cam.rotation_euler=(Vector((1.85,0,1.8))-cam.location).to_track_quat('-Z','Y').to_euler()
cam.data.type='ORTHO';cam.data.ortho_scale=7.2;scene.camera=cam
scene.render.filepath=str(ROOT/'tests/assets/local_only/pose_review.png')
bpy.ops.render.render(write_still=True)
print('POSE_REVIEW',scene.render.filepath)
