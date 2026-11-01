"""Opt-in deletion coordination; NOT wired to the product and never removes data.

Security contract: Linux only, on a local filesystem with working flock, atomic
O_EXCL and durable file/directory fsync. The caller must provision an existing,
private, trusted root and protect it, all ancestors, and .deletion-lifecycle
against rename, deletion, mount replacement and modification by untrusted
actors for the entire lifetime of this protocol. All cooperating processes
must use the same root and keep execution_lease open for *all* object work,
including publication. Old/non-cooperating processes are not fenced.

This module does not validate or traverse object directories, and cannot make
physical deletion safe. Tombstones and lock files must never be removed, even
after object removal or restore. Backups/restores must preserve them: loss or
rollback destroys the no-reuse guarantee. IDs are lowercase 32-character hex;
root, kind and ID together identify an object. Filesystem and hardware must
honour fsync; errors propagate and success must not be inferred from failure.
Before a tombstone call returns successfully, power loss can erase its marker;
a separate durable deletion intent/recovery protocol must prevent new work
until such an uncertain operation is retried. This module does not supply that
external intent log. Windows, including every reparse point/junction/symlink
case, is unsupported and fails closed before touching any path.
"""
from contextlib import contextmanager
import os
import re
import stat
import sys


class UnsupportedPlatform(RuntimeError):
    """The platform cannot meet this implementation's safety contract."""


class Tombstoned(RuntimeError):
    """The object identity has been permanently retired."""


_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_INFRA = ".deletion-lifecycle"


def _supported():
    if sys.platform != "linux" or not all(hasattr(os, x) for x in
            ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC")):
        raise UnsupportedPlatform("requires supported Linux dirfd/flock semantics")


def _check_dir(fd):
    s = os.fstat(fd)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o077:
        raise PermissionError("lifecycle root and infrastructure must be private owner-only directories")


def _open_root(root):
    path = os.fspath(root)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("root must be an absolute text path")
    parts = path.split("/")
    if any(p in (".", "..") for p in parts):
        raise ValueError("dot path components are forbidden")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in filter(None, parts):
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        _check_dir(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular(parent, name, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or
                s.st_uid != os.geteuid() or s.st_mode & 0o077):
            raise PermissionError("unsafe lifecycle file")
        return fd
    except BaseException:
        os.close(fd)
        raise


class DeletionLifecycle:
    """Coordinator for one trusted root. No object paths are accepted."""

    def __init__(self, root):
        _supported()
        # Retain the spelling, not an open descriptor: each operation revalidates.
        self._root = os.fspath(root)
        fd = _open_root(self._root)
        os.close(fd)

    @staticmethod
    def _key(kind, object_id):
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ValueError("invalid kind")
        if not isinstance(object_id, str) or not _ID.fullmatch(object_id):
            raise ValueError("ID must be 32 lowercase hexadecimal characters")
        # '-' cannot appear in an ID, so this encoding is unambiguous.
        return kind + "-" + object_id

    @contextmanager
    def _locked(self, kind, object_id):
        import fcntl
        key = self._key(kind, object_id)
        root = _open_root(self._root)
        infra = lock = None
        try:
            try:
                os.mkdir(_INFRA, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            infra = os.open(_INFRA, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=root)
            _check_dir(infra)
            # Also sync on recovery after a creator crashed following mkdir.
            os.fsync(root)
            lock = _open_regular(infra, key + ".lock", create=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A stable lock inode is part of the persistent protocol state.
            os.fsync(lock)
            os.fsync(infra)
            yield infra, key
        finally:
            if lock is not None:
                os.close(lock)
            if infra is not None:
                os.close(infra)
            os.close(root)

    @staticmethod
    def _has_tombstone(infra, key):
        try:
            fd = _open_regular(infra, key + ".tombstone")
        except FileNotFoundError:
            return False
        else:
            os.close(fd)
            return True

    @contextmanager
    def execution_lease(self, kind, object_id):
        """Exclusive cross-process lease; raises Tombstoned before any work.

        Hold until every associated write/publication has finished. A process
        crash releases flock. These leases serialize even ordinary executions.
        """
        with self._locked(kind, object_id) as (infra, key):
            if self._has_tombstone(infra, key):
                raise Tombstoned(key)
            yield

    def tombstone(self, kind, object_id):
        """Permanently retire an identity, waiting for prior leases to finish.

        Idempotent, including recovery after an interrupted creation. Return
        only after syncing the tombstone and its directory. An exception means
        the outcome may be uncertain; retry, never assume the ID is reusable.
        This method does not delete object data.
        """
        with self._locked(kind, object_id) as (infra, key):
            name = key + ".tombstone"
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=infra)
            except FileExistsError:
                fd = _open_regular(infra, name)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(infra)
