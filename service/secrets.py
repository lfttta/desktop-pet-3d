"""Encrypted API-key storage for the local single-user service.

On Windows the key is protected with the current user's DPAPI profile and
written to ``<data-dir>/secrets.bin``. The file never contains plaintext:
unavailable platforms and unreadable blobs raise explicit errors instead of
falling back to a plain file.
"""

import ctypes
import os
from pathlib import Path
import sys
import tempfile

MAGIC = b'PET3D-SECRETS-1\n'
MAX_KEY_BYTES = 16384
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretsError(Exception):
    """Explicit credential-storage failure with a protocol-friendly code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.c_void_p)]


def _win32_api():
    if sys.platform != 'win32':
        raise SecretsError('secrets_unsupported',
                           'Secure key storage is only available on Windows.')
    cached = getattr(_win32_api, '_cached', None)
    if cached is not None:
        return cached
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(_DataBlob),
    ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _win32_api._cached = (crypt32, kernel32)
    return _win32_api._cached


def _protect(data):
    crypt32, kernel32 = _win32_api()
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise SecretsError('secrets_unavailable',
                           'Windows DPAPI could not encrypt the key.')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(data):
    crypt32, kernel32 = _win32_api()
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    blob_out = _DataBlob()
    description = ctypes.c_void_p()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), ctypes.byref(description), None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    try:
        if not ok:
            raise SecretsError('secrets_unreadable',
                               'Stored key could not be decrypted for this user.')
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)
        if description.value:
            kernel32.LocalFree(description)


def _atomic_write(path, content):
    descriptor, name = tempfile.mkstemp(prefix='.secrets-', suffix='.tmp',
                                        dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class SecretStore:
    """Encrypted key file owned by one data directory."""

    def __init__(self, directory):
        self.path = Path(directory) / 'secrets.bin'

    @property
    def available(self):
        return sys.platform == 'win32'

    def save(self, key):
        if type(key) is not str or not key:
            raise SecretsError('invalid_key', 'An API key string is required.')
        data = key.encode('utf-8')
        if len(data) > MAX_KEY_BYTES:
            raise SecretsError('invalid_key', 'The API key is too long.')
        blob = _protect(data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, MAGIC + blob)
        except OSError as exc:
            raise SecretsError('secrets_unavailable',
                               'The key could not be saved securely.') from exc

    def load(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise SecretsError('secrets_missing',
                               'No API key is configured.') from exc
        except OSError as exc:
            raise SecretsError('secrets_unreadable',
                               'The stored key could not be read.') from exc
        if not raw.startswith(MAGIC):
            raise SecretsError('secrets_unreadable',
                               'The stored key file is not recognized.')
        blob = raw[len(MAGIC):]
        if not blob:
            raise SecretsError('secrets_unreadable',
                               'The stored key file is empty.')
        if sys.platform != 'win32':
            raise SecretsError('secrets_unsupported',
                               'Secure key storage is only available on Windows.')
        try:
            data = _unprotect(blob)
            key = data.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise SecretsError('secrets_unreadable',
                               'The stored key is not valid text.') from exc
        if not key:
            raise SecretsError('secrets_unreadable',
                               'The stored key is empty.')
        return key

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise SecretsError('secrets_unreadable',
                               'The stored key could not be removed.') from exc
        return True


def secret_store_for(store):
    """Return the store's injected secret backend or the default data-dir file."""
    existing = getattr(store, 'secrets_store', None)
    if existing is not None:
        return existing
    path = getattr(store, 'path', None)
    directory = Path(path).parent if path is not None else Path('.')
    return SecretStore(directory)


def load_key_for(store):
    """Load the configured key without ever persisting or echoing it."""
    supplier = getattr(store, 'ai_key_provider', None)
    if supplier is not None:
        value = supplier()
        if type(value) is not str or not value.strip():
            raise SecretsError('secrets_missing', 'No API key is configured.')
        return value
    return secret_store_for(store).load()
