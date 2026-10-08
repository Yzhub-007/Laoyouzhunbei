"""Offline startup diagnostics: never imports the app or reads protected runtime data."""
from __future__ import annotations

import argparse
import ctypes
import getpass
import importlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def current_username() -> str:
    if os.name == 'nt':
        size = ctypes.c_ulong(257)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise OSError('无法确认当前 Windows 账户。')
        return buffer.value
    return getpass.getuser()


def run_checks(root: Path = ROOT, port: int = 8765, *, check_startup: bool = False,
               skip_port: bool = False) -> dict:
    """Only probes root writability, in-memory SQLite, imports, identity, and loopback bind."""
    checks = []

    def add(name, status, message):
        checks.append({'name': name, 'status': status, 'message': message})

    add('windows', 'pass' if os.name == 'nt' else 'error',
        '当前为 Windows；加密数据将绑定当前 Windows 账户。' if os.name == 'nt' else '正式运行需要 Windows；不支持以明文存储降级运行。')
    add('python', 'pass' if sys.version_info >= (3, 11) else 'error',
        f'Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}；要求 3.11+。')
    try:
        username = current_username()
        sandbox = username.casefold().startswith('codexsandbox')
        add('account', ('error' if check_startup else 'warning') if sandbox else 'pass',
            '当前是 Codex 沙箱账户；正式项目必须由日常 Windows 账户启动，避免加密数据绑定到临时账户。'
            if sandbox else '当前为普通系统账户；请始终使用同一个日常 Windows 账户运行项目。')
    except OSError:
        add('account', 'error', '无法确认运行账户，已停止正式启动。')
    try:
        with sqlite3.connect(':memory:') as source, sqlite3.connect(':memory:') as target:
            source.execute('CREATE TABLE probe (value INTEGER)')
            source.execute('INSERT INTO probe VALUES (1)')
            target.deserialize(source.serialize())
            if target.execute('SELECT value FROM probe').fetchone() != (1,):
                raise ValueError('SQLite round trip failed')
            if target.execute("SELECT json_extract('{\"ready\":1}', '$.ready')").fetchone() != (1,):
                raise ValueError('SQLite JSON support missing')
        add('sqlite', 'pass', 'SQLite serialize/deserialize 与版本检索所需 JSON 函数检查通过。')
    except (AttributeError, sqlite3.Error, ValueError):
        add('sqlite', 'error', '当前 SQLite 缺少可用的 serialize/deserialize 或 JSON 函数；请使用标准 Python 3.11+ 安装。')
    try:
        pillow = importlib.import_module('PIL')
        importlib.import_module('PIL.Image')
        parts = tuple(int(x) for x in pillow.__version__.split('.')[:3])
        if parts < (12, 3, 0):
            add('pillow', 'error', f'Pillow {pillow.__version__} 版本过旧；请运行 setup.ps1 安装锁定版本 12.3.0。')
        else:
            add('pillow', 'pass' if pillow.__version__ == '12.3.0' else 'warning',
                f'Pillow {pillow.__version__} 可加载；交付版本锁定为 12.3.0。')
    except (ImportError, AttributeError, ValueError):
        add('pillow', 'error', '未找到可用的 Pillow。请运行 setup.ps1；摄像头图像预处理不能省略此依赖。')
    required = ('server.py', 'secure_storage.py', 'auth.py', 'web/index.html', 'data/demo.json')
    missing = [name for name in required if not (root / name).is_file()]
    add('files', 'error' if missing else 'pass',
        '缺少启动文件：' + '、'.join(missing) if missing else '启动文件完整。')
    try:
        with tempfile.NamedTemporaryFile(prefix='.doctor-', dir=root):
            pass
        add('writable', 'pass', '项目目录可写；没有读取或修改 runtime 数据。')
    except OSError:
        add('writable', 'error', '项目目录不可写。请解压到自己的本地文件夹，不要在压缩包或只读目录中启动。')
    if not skip_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', port))
            add('port', 'pass', f'本机端口 {port} 可绑定，探测后已立即释放。')
        except OSError:
            add('port', 'error', f'本机端口 {port} 被占用或无法绑定；关闭旧服务，或以 -Port 指定其他端口。')
    return {'ok': not any(item['status'] == 'error' for item in checks),
            'offline': True, 'runtime_read': False, 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description='离线环境自检，不读取密钥、账户、监控画面或运行数据库')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--skip-port', action='store_true')
    parser.add_argument('--check-startup', action='store_true', help='正式启动时拒绝临时沙箱账户')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 到 65535 之间')
    result = run_checks(args.root, args.port, check_startup=args.check_startup, skip_port=args.skip_port)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result['checks']:
            print(f"[{item['status'].upper()}] {item['message']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
