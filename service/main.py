"""Console-subsystem entry point for Godot execute_with_pipe and PyInstaller."""

import argparse
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service.protocol import ProtocolError, emit_error
from service.runtime import serve


class Parser(argparse.ArgumentParser):
    def print_help(self, file=None):
        super().print_help(file=sys.stderr)

    def error(self, message):
        raise ValueError('Invalid arguments; use --data-dir PATH.')


def main(argv=None):
    parser = Parser(description='Pet3D UTF-8 JSON-lines service (protocol on stdout).')
    parser.add_argument('--data-dir', type=Path)
    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        emit_error(sys.stdout.buffer, ProtocolError('invalid_arguments', str(exc)))
        return 2
    directory = args.data_dir
    if directory is None:
        local = os.environ.get('LOCALAPPDATA')
        directory = (Path(local) if local else Path.home() / 'AppData/Local') / 'Pet3D'
    try:
        return serve(directory, sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)
    except BrokenPipeError:
        # Avoid a second buffered flush failure during interpreter shutdown.
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == '__main__':
    # The reader thread is a daemon blocked on stdin. A normal interpreter
    # shutdown can race with it holding the buffered-stdin lock and print a
    # "Fatal Python error: _enter_buffered_busy" after a clean stop. Flush the
    # protocol stream and leave without joining interpreter finalization.
    exit_code = main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError, AttributeError):
        pass
    os._exit(exit_code)
