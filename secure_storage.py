"""Small local stores encrypted with Windows DPAPI (CurrentUser).

SQLite only runs in memory. Nothing in this module writes plaintext database or
JSON staging files. The Windows account boundary remains the trust boundary:
another process running as this account can also call DPAPI.
"""
import copy
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager

MAGIC = b'CAMPUS-DPAPI-V1\x00'
MAX_BYTES = 64 * 1024 * 1024
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_ACTIVE = threading.local()


class StorageSecurityError(OSError):
    """Encryption or access restrictions could not be enforced; fail closed."""


def _windows():
    if os.name != 'nt':
        raise StorageSecurityError('安全存储需要 Windows DPAPI；未启用明文兼容模式。')


class _Blob(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    array = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _Blob(len(data), array), array


def _dpapi(data, purpose, *, decrypt=False):
    _windows()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    incoming, keep_input = _blob(data)
    entropy, keep_entropy = _blob(b'CampusMonitor:V1:' + purpose.encode('ascii'))
    outgoing = _Blob()
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        second = None
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        second = 'Campus monitor protected storage'
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
    if not function(ctypes.byref(incoming), second, ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing)):
        raise StorageSecurityError('Windows 安全存储加密或解密失败；请使用原 Windows 账户恢复。')
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.length)
            kernel.LocalFree(outgoing.data)


def _current_sid():
    _windows()
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token, size = wintypes.HANDLE(), wintypes.DWORD()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise StorageSecurityError('无法读取当前 Windows 账户，安全存储已停止。')
    try:
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise StorageSecurityError('无法确认 Windows 文件权限。')
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise StorageSecurityError('无法确认 Windows 文件权限。')
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def protect_file(path):
    """Replace this file/directory DACL with current-user and SYSTEM access only."""
    _windows()
    path = Path(path)
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise StorageSecurityError('安全存储不能使用链接或目录联接。')
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    inherit = 'OICI' if path.is_dir() else ''
    descriptor = ctypes.c_void_p()
    sddl = f'D:P(A;{inherit};FA;;;{_current_sid()})(A;{inherit};FA;;;SY)'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise StorageSecurityError('无法创建受限文件权限。')
    try:
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present:
            raise StorageSecurityError('无法创建受限文件权限。')
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
        if adv.SetNamedSecurityInfoW(str(path.resolve()), 1, 0x80000004, None, None, dacl, None):
            raise StorageSecurityError('无法限制安全存储文件权限；请使用支持 NTFS 权限的目录。')
    finally:
        kernel.LocalFree(descriptor)


def _bounded_read(path):
    with path.open('rb') as handle:
        raw = handle.read(MAX_BYTES + 65537)
    if len(raw) > MAX_BYTES + 65536:
        raise StorageSecurityError('安全存储文件超过 64 MiB 限制。')
    return raw


@contextmanager
def _locked(path):
    _windows()
    original = Path(path).absolute()
    if original.is_symlink() or (hasattr(original, 'is_junction') and original.is_junction()):
        raise StorageSecurityError('安全存储不能使用文件链接。')
    path = original.resolve()
    key = os.path.normcase(str(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        active = getattr(_ACTIVE, 'paths', set())
        if key in active:
            raise StorageSecurityError('不能在同一事务中重复打开同一安全存储。')
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + '.lock')
        if lock_path.is_symlink():
            raise StorageSecurityError('安全存储锁不能使用文件链接。')
        import msvcrt
        with lock_path.open('a+b') as handle:
            protect_file(lock_path)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise StorageSecurityError('安全存储正在使用中，请稍后重试。')
                    time.sleep(0.025)
            _ACTIVE.paths = active | {key}
            try:
                if path.exists():
                    protect_file(path)
                yield path
            finally:
                _ACTIVE.paths = active
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _seal(raw, purpose):
    if len(raw) > MAX_BYTES:
        raise StorageSecurityError('安全存储内容超过 64 MiB 限制。')
    return MAGIC + purpose.encode('ascii') + b'\0' + _dpapi(raw, purpose)


def _unseal(raw, purpose):
    prefix = MAGIC + purpose.encode('ascii') + b'\0'
    if not raw.startswith(prefix):
        raise StorageSecurityError('安全存储格式不匹配，未尝试以明文读取。')
    plain = _dpapi(raw[len(prefix):], purpose, decrypt=True)
    if len(plain) > MAX_BYTES:
        raise StorageSecurityError('安全存储内容超过 64 MiB 限制。')
    return plain


def _atomic_write(path, encrypted):
    descriptor, temp_name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            protect_file(temporary)
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        protect_file(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _migration_backup(path, raw, purpose):
    backup = path.with_name(path.name + '.migration.dpapi')
    if backup.exists():
        # Never overwrite the initial recovery point, and verify its protection.
        protect_file(backup)
        _unseal(_bounded_read(backup), purpose)
    else:
        _atomic_write(backup, _seal(raw, purpose))


@contextmanager
def database(path):
    """Open a serializable in-memory SQLite transaction, then atomically encrypt."""
    with _locked(path) as path:
        stored = _bounded_read(path) if path.exists() else None
        legacy = stored is not None and not stored.startswith(MAGIC)
        if legacy and not stored.startswith(b'SQLite format 3\x00'):
            raise StorageSecurityError('数据库不是有效的受保护文件或旧 SQLite 文件。')
        for suffix in ('-wal', '-journal'):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise StorageSecurityError('发现未合并的 SQLite 日志；请先停止旧服务并完成 checkpoint 后迁移。')
        db = sqlite3.connect(':memory:', timeout=5)
        db.row_factory = sqlite3.Row
        initial = None
        try:
            if legacy:
                # Read-only immutable mode never creates a plaintext journal.
                # The service must be stopped before migrating a legacy store.
                old = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
                try:
                    old.backup(db)
                finally:
                    old.close()
            elif stored is not None:
                initial = _unseal(stored, 'sqlite')
                db.deserialize(initial)
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA journal_mode=MEMORY')
            db.execute('PRAGMA secure_delete=ON')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise StorageSecurityError('数据库完整性检查失败；未覆盖原文件。')
            # A brand-new empty database needs one page before serialization.
            # Rewriting this PRAGMA on an existing database changes SQLite's
            # header counter, forcing a full encrypted write even for SELECTs.
            if stored is None:
                db.execute('PRAGMA user_version=0')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            raw = db.serialize()
            if legacy:
                _migration_backup(path, stored, 'sqlite')
            if raw != initial:
                _atomic_write(path, _seal(raw, 'sqlite'))
        finally:
            db.close()


def _decode_json(raw):
    def invalid_constant(value):
        raise ValueError('JSON contains a non-finite number')
    return json.loads(raw, parse_constant=invalid_constant)


def read_json(path, default=None):
    with _locked(path) as path:
        if not path.exists():
            return copy.deepcopy(default)
        stored = _bounded_read(path)
        legacy = not stored.startswith(MAGIC)
        raw = stored if legacy else _unseal(stored, 'json')
        data = _decode_json(raw)
        if legacy:
            _migration_backup(path, raw, 'json')
            _atomic_write(path, _seal(raw, 'json'))
        return data


def write_json(path, data):
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    with _locked(path) as path:
        if path.exists():
            previous = _bounded_read(path)
            if not previous.startswith(MAGIC):
                _decode_json(previous)
                _migration_backup(path, previous, 'json')
        _atomic_write(path, _seal(raw, 'json'))
