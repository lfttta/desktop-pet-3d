"""Offline Windows speech synthesis with a small on-disk cache.

The service may only use the standard library, so synthesis is delegated to the
``System.Speech.Synthesis`` API that ships with Windows PowerShell. Rendered
WAV files are cached under ``<data-dir>/tts`` and keyed by a SHA-1 digest of the
spoken text and the effective playback parameters, so replaying a line neither
starts PowerShell nor re-renders it.

``_run_powershell`` is the only process boundary in this module. Tests replace
it; nothing else here spawns a process or touches audio devices.
"""

import base64
import hashlib
import os
from pathlib import Path
import subprocess

from service.protocol import ProtocolError

MAX_TEXT_CHARS = 500
RATE_MIN = -10
RATE_MAX = 10
VOLUME_MIN = 0
VOLUME_MAX = 100
TIMEOUT_SECONDS = 20
POWERSHELL = 'powershell.exe'
CACHE_DIRNAME = 'tts'

# Text and voice travel through the environment instead of the command line:
# no PowerShell quoting rules apply and the argument list stays short. The
# script itself is passed as UTF-16LE/base64, the form -EncodedCommand expects.
_POWERSHELL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synthesizer.Rate = [int]$env:PET3D_TTS_RATE
    $synthesizer.Volume = [int]$env:PET3D_TTS_VOLUME
    if ($env:PET3D_TTS_VOICE) { $synthesizer.SelectVoice($env:PET3D_TTS_VOICE) }
    $synthesizer.SetOutputToWaveFile($env:PET3D_TTS_OUTPUT)
    $synthesizer.Speak($env:PET3D_TTS_TEXT)
} finally {
    $synthesizer.Dispose()
}
'''


def _bounded_integer(value, low, high, default):
    """Return an int inside [low, high]; unusable values fall back to default."""
    if type(value) is bool or not isinstance(value, (int, float)):
        return default
    try:
        number = int(value)
    except (OverflowError, ValueError):
        return default
    return max(low, min(high, number))


def _normalize_text(value):
    if type(value) is not str:
        raise ProtocolError('invalid_request', 'tts.synthesize requires text.')
    text = value.strip()
    if not text:
        raise ProtocolError('invalid_request', 'tts.synthesize requires non-empty text.')
    return text[:MAX_TEXT_CHARS]


def _cache_destination(data_dir, text, voice, rate, volume):
    digest = hashlib.sha1()
    for part in (text, voice, str(rate), str(volume)):
        digest.update(part.encode('utf-8'))
        digest.update(b'\x00')
    directory = Path(data_dir).expanduser().resolve() / CACHE_DIRNAME
    return directory, directory / (digest.hexdigest() + '.wav')


def _run_powershell(text, destination, voice, rate, volume):
    """Render ``text`` into ``destination``; raise on any backend failure."""
    encoded = base64.b64encode(_POWERSHELL_SCRIPT.encode('utf-16-le')).decode('ascii')
    command = [POWERSHELL, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    environment = dict(os.environ)
    environment.update({
        'PET3D_TTS_TEXT': text,
        'PET3D_TTS_OUTPUT': str(destination),
        'PET3D_TTS_VOICE': voice,
        'PET3D_TTS_RATE': str(rate),
        'PET3D_TTS_VOLUME': str(volume),
    })
    subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=TIMEOUT_SECONDS, check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def synthesize(text, data_dir, voice='', rate=0, volume=70):
    """Synthesize ``text`` to a WAV file and return its path.

    ``rate`` is clamped to -10..10 and ``volume`` to 0..100. An empty or
    whitespace-only text raises ``invalid_request``; any failure to run the
    Windows speech backend — missing PowerShell, no System.Speech, a timeout or
    a nonzero exit — is reported as ``tts_unavailable``.
    """
    spoken = _normalize_text(text)
    voice = voice.strip() if type(voice) is str else ''
    rate = _bounded_integer(rate, RATE_MIN, RATE_MAX, 0)
    volume = _bounded_integer(volume, VOLUME_MIN, VOLUME_MAX, 70)
    try:
        directory, destination = _cache_destination(data_dir, spoken, voice, rate, volume)
    except (OSError, TypeError, ValueError):
        raise ProtocolError('tts_unavailable', 'Speech synthesis is unavailable.') from None
    try:
        cached = destination.is_file() and destination.stat().st_size > 0
    except OSError:
        cached = False
    if cached:
        return {'path': str(destination), 'cached': True, 'voice': voice}
    temporary = destination.with_name(destination.name + '.part')
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        _run_powershell(spoken, temporary, voice, rate, volume)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise OSError('speech synthesis produced no audio')
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProtocolError('tts_unavailable', 'Speech synthesis is unavailable.') from None
    return {'path': str(destination), 'cached': False, 'voice': voice}
