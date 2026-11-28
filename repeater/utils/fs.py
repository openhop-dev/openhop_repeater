"""Filesystem helpers: detect whether a directory lives on RAM-backed storage."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("FS")

# Filesystems whose content is lost at restart (linux/magic.h).
TMPFS_MAGIC = 0x01021994
RAMFS_MAGIC = 0x858458F6
_VOLATILE_MAGICS = frozenset({TMPFS_MAGIC, RAMFS_MAGIC})

_libc: ctypes.CDLL | None = None
_libc_loaded = False


def _get_libc() -> ctypes.CDLL | None:
    global _libc, _libc_loaded
    if not _libc_loaded:
        _libc_loaded = True
        try:
            _libc = ctypes.CDLL(None, use_errno=True)
        except OSError:
            _libc = None
    return _libc


def _fstatfs_type(fd: int) -> int | None:
    """Return ``f_type`` of the filesystem holding ``fd``, or None on failure."""
    libc = _get_libc()
    if libc is None:
        return None
    # f_type is the first native long of Linux struct statfs. An oversized buffer
    # avoids reproducing the architecture-specific layout.
    buffer = (ctypes.c_long * 64)()
    if libc.fstatfs(ctypes.c_int(fd), ctypes.byref(buffer)) != 0:
        return None
    return buffer[0] & 0xFFFFFFFF


def _nearest_existing_dir(path: Path) -> Path | None:
    current = path
    while True:
        if current.is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


def is_volatile_fs(path: os.PathLike | str) -> bool:
    """Return True if ``path`` lives on a RAM-backed filesystem (tmpfs or ramfs).

    Any other case returns False: persistent filesystem, non-Linux platform, missing
    directory, failed syscall. This function never raises.

    ``fstatfs`` reports the mount actually under the path in the caller's mount
    namespace, so bind mounts and tmpfs mounts in a container are seen correctly.
    The nearest existing parent is used when ``path`` does not exist yet.
    """
    if sys.platform != "linux":
        return False
    try:
        directory = _nearest_existing_dir(Path(path).expanduser())
        if directory is None:
            return False
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            magic = _fstatfs_type(fd)
        finally:
            os.close(fd)
        return magic in _VOLATILE_MAGICS
    except Exception as e:  # noqa: BLE001 (detection must never break callers)
        logger.debug(f"Filesystem detection failed for {path}: {e}")
        return False
