"""Asset delivery contracts, independently checked after conversion and baking."""
import json
import pathlib
import struct
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STAGE = ROOT/'prototypes/stage0'


class AssetContract(unittest.TestCase):
    def test_runtime_character_libraries_exist(self):
        for name in ('godette', 'swordsman'):
            path = STAGE/f'assets_data/{name}.json'
            self.assertTrue(path.is_file(), str(path))
            config = json.loads(path.read_text(encoding='utf-8'))
            self.assertTrue((STAGE/config['animation_library'].removeprefix('res://')).is_file())
            self.assertTrue(config['bone_map']['Hips'])
            self.assertGreaterEqual(len(config['bone_map']), 17)

    def test_source_clips_are_meters_and_named(self):
        for name in ('idle', 'wave'):
            data = (STAGE/f'motions/{name}.glb').read_bytes()
            size = struct.unpack_from('<I',data,12)[0]
            doc = json.loads(data[20:20+size])
            rig = next(n for n in doc['nodes'] if n.get('name') == 'SourceRig')
            self.assertEqual(rig['scale'],[1,1,1])
            self.assertEqual(rig['rotation'],[0,0,0,1])
            hip = doc['nodes'][rig['children'][0]]
            self.assertAlmostEqual(hip['translation'][1],1.00052,places=4)
            self.assertEqual([a['name'] for a in doc['animations']],[name])
            self.assertEqual(len(doc['skins'][0]['joints']),65)

    def test_restricted_vrm1_has_no_packaged_copy(self):
        self.assertFalse(any('avatar' in p.name.lower() and p.suffix in ('.vrm','.glb','.scn','.tscn') for p in STAGE.rglob('*')))
        path = STAGE/'assets_data/avatar_sample_m.local.json'
        self.assertTrue(path.is_file())
        config = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(config['publishable'])
        self.assertFalse(config['enabled_by_default'])
        self.assertTrue(pathlib.Path(config['model_path']).is_absolute())


if __name__ == '__main__':
    unittest.main()
