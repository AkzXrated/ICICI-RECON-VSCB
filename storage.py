"""Thread-safe, crash-safe JSON persistence helpers.

Every read-modify-write sequence on a persistence file MUST be wrapped in
`with path_lock(path):` so concurrent Flask requests cannot interleave.
Writes go to a temp file, are fsync'd, then atomically swapped in with
os.replace(), so a crash mid-write can never truncate the live file.
User-entered state (manual links, sticky notes, aliases) additionally keeps
a .bak copy of the previous good version, which load_json() falls back to.
"""
import json
import logging
import os
import shutil
import threading

logger = logging.getLogger(__name__)

_locks = {}
_locks_guard = threading.Lock()


class CorruptJSONError(Exception):
    """Raised when a JSON file exists but cannot be parsed (and no backup could be recovered)."""


def path_lock(path):
    """Return a reentrant lock unique to this file path."""
    key = os.path.normcase(os.path.abspath(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def load_json(path, default=None):
    """Parse JSON at `path`. Missing file -> `default`.
    Corrupt file -> recover from .bak if possible, else raise CorruptJSONError
    (never silently returns `default` for an existing-but-broken file, so
    callers can't accidentally overwrite real data with an empty structure)."""
    with path_lock(path):
        if not os.path.exists(path):
            return default
        # utf-8-sig transparently accepts files with or without a UTF-8 BOM
        # (hand-edited files saved by Notepad/PowerShell often carry one)
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            bak = path + '.bak'
            if os.path.exists(bak):
                try:
                    with open(bak, 'r', encoding='utf-8-sig') as f:
                        data = json.load(f)
                    logger.warning("Recovered %s from backup after load failure: %s", path, e)
                    return data
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    logger.error("Backup %s is also unreadable", bak)
            logger.error("Failed to load %s: %s", path, e)
            raise CorruptJSONError(f"{os.path.basename(path)} is corrupt: {e}") from e


def _atomic_write(path, text):
    import time
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows retry loop for PermissionError caused by AV or indexer
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                break
            except PermissionError as pe:
                if attempt == 3:
                    raise pe
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def save_json(path, obj, backup=True, indent=2):
    """Atomically serialize `obj` to `path`. With backup=True the previous
    version is kept as `path`.bak before being replaced, preserving .bak if current file is corrupt."""
    with path_lock(path):
        text = json.dumps(obj, indent=indent)
        if backup and os.path.exists(path):
            try:
                # Only copy to .bak if path is valid non-empty JSON to avoid overwriting a good backup with corruption
                with open(path, 'r', encoding='utf-8-sig') as f:
                    json.load(f)
                shutil.copy2(path, path + '.bak')
            except Exception as e:
                logger.warning("Skipping backup of %s because current file is not valid JSON (%s); existing backup preserved.", path, e)
        _atomic_write(path, text)


def save_text(path, text):
    """Atomically write pre-serialized text (for large caches with custom encoders)."""
    with path_lock(path):
        _atomic_write(path, text)
