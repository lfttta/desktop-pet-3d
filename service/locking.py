"""OS-held data-directory lock, automatically released after a process crash."""

import os


class AlreadyRunning(OSError):
    pass


class DataDirectoryLock:
    def __init__(self, directory):
        self.directory = directory
        self.stream = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / 'service.lock').open('a+b')
        try:
            self.stream.seek(0, os.SEEK_END)
            if self.stream.tell() == 0:
                self.stream.write(b'\0')
                self.stream.flush()
            self.stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunning('data directory is already in use') from exc
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self, *exception):
        self.stream.close()

