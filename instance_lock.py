"""Hold one Windows service instance per runtime directory until process exit.

This lock is separate from individual protected-store transactions so local
security administration remains available while the service is running.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import threading

from secure_storage import StorageSecurityError, protect_file

LOCK_NAME = '.service-instance.lock'
_ACTIVE = set()
_GUARD = threading.Lock()


class InstanceRunningError(RuntimeError):
    """Another service already owns this runtime directory."""


def _reject_links(path):
    # Check every existing component before resolving: a linked ancestor is as
    # unsafe as a linked runtime or lock file. st_file_attributes works on 3.11,
    # which does not yet expose Path.is_junction().
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise StorageSecurityError('服务运行目录及锁文件不能使用链接或目录联接。')


@contextmanager
def running_instance(runtime):
    """Acquire immediately or fail; leave the harmless lock file after release."""
    if os.name != 'nt':
        raise StorageSecurityError('服务实例保护需要 Windows 文件锁。')
    import msvcrt

    original = Path(runtime).absolute()
    _reject_links(original)
    path = original.resolve()
    key = os.path.normcase(str(path))
    with _GUARD:
        if key in _ACTIVE:
            raise InstanceRunningError('此运行目录已有服务正在运行；请先停止原服务，不要更换端口重复启动。')
        _ACTIVE.add(key)
    try:
        path.mkdir(parents=True, exist_ok=True)
        _reject_links(path)
        protect_file(path)
        lock_path = path / LOCK_NAME
        _reject_links(lock_path)
        with lock_path.open('a+b') as handle:
            protect_file(lock_path)
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise InstanceRunningError('此运行目录已有服务正在运行；请先停止原服务，不要更换端口重复启动。') from None
                raise StorageSecurityError('无法取得服务实例锁；请检查运行目录权限和文件系统。') from exc
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'\0')
                    handle.flush()
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        with _GUARD:
            _ACTIVE.discard(key)
