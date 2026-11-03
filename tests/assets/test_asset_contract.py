"""Verify the preserved repository baseline without old external projects."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_original_godette_cache_is_preserved():
    data = (ROOT / 'app/motions/cache/godette.tres').read_bytes()
    assert hashlib.sha256(data).hexdigest() == 'e9f0032d2e4d3f22e66b1931b0ca42b4bb91056ef6c739023f7d909c03dffa64'


def test_original_model_is_preserved():
    data = (ROOT / 'app/models/godette.vrm').read_bytes()
    assert hashlib.sha256(data).hexdigest() == '0b008f60c808eb1c83781c650e654a70d512574932e3867f80609c62d7052c6c'
