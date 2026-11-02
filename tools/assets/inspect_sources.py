"""Read-only glTF/VRM inventory. Does not import or rewrite source files."""
import hashlib
import json
import pathlib
import struct

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHARACTERS = pathlib.Path(r'D:\danjigame\模型all\角色')
OLD_MODELS = pathlib.Path(r'D:\danjigame\3D-pet\work-pet\prototypes\azpepoze\models')


def read_glb(path):
    data = pathlib.Path(path).read_bytes()
    assert data[:4] == b'glTF'
    size = struct.unpack_from('<I', data, 12)[0]
    return json.loads(data[20:20+size])


def inventory(path):
    doc = read_glb(path)
    nodes = doc.get('nodes', [])
    return {
        'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'asset': doc.get('asset'), 'extensions_used': doc.get('extensionsUsed', []),
        'skins': [dict(name=s.get('name'), joints=[nodes[i].get('name', str(i)) for i in s['joints']]) for s in doc.get('skins', [])],
        'nodes': [dict(index=i, **n) for i, n in enumerate(nodes)],
        'meshes': [dict(name=m.get('name'), targetNames=m.get('extras', {}).get('targetNames', []), primitives=len(m['primitives'])) for m in doc.get('meshes', [])],
        'vrm': doc.get('extensions', {}).get('VRM', doc.get('extensions', {}).get('VRMC_vrm')),
        'animations': [a.get('name') for a in doc.get('animations', [])],
    }


if __name__ == '__main__':
    paths = list(CHARACTERS.glob('*.glb')) + list(OLD_MODELS.glob('*.vrm')) + [OLD_MODELS/'soldier.glb']
    records = [inventory(p) for p in paths]
    out = ROOT/'characters/source_inventory.json'
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    for r in records:
        print(json.dumps({k:r[k] for k in ('path','sha256','meshes')}))
        if r['vrm']:
            print(json.dumps(r['vrm'].get('meta')))
