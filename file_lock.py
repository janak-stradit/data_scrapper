"""Cross-process lock for the JSON files under output/ that more than one
process writes — the web server and the scheduler run in separate
containers sharing that directory (docker-compose.yml), so the per-module
threading locks alone no longer cover every writer.

An advisory flock() on a sidecar ".lock" file. Windows has no fcntl; there
it's a no-op — fine for local dev, where `serve` runs the scheduler inside
the same process and the threading locks still serialize everything.
"""
import contextlib
import os

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None


@contextlib.contextmanager
def locked(path: str):
    """Hold an exclusive lock on `path` (created if missing) for the block."""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
