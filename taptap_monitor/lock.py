from pathlib import Path
import os


class ProcessLock:
    """One writer process per database; status and SQLite backups can still run."""
    def __init__(self, database):
        path = Path(database).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")

    def __enter__(self):
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another monitor process owns this database; use the running service's /api/scan endpoint") from exc
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()
