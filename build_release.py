"""Build a clean source release without inspecting private runtime/evidence trees."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
TOP_LEVEL = 'Laoyouzhunbei-v0.9-V-FloodNet'
ROOT_FILES = {'.env.example', '.gitignore', 'README.md', 'requirements.txt', 'start.ps1', 'start.cmd',
              'start-with-vfloodnet.ps1', 'setup.ps1'}
DIR_EXTENSIONS = {
    'web': {'.html', '.css', '.js', '.svg'},
    'docs': {'.md'},
    'tests': {'.py', '.cjs'},
    'integrations': {'.py', '.ps1', '.txt', '.md'},
    'third_party': {'.py', '.md', '.txt', '.sh', '.yaml', '.yml', '.json', '.jpg', '.jpeg', '.png', '.gif'},
}
DATA_FILES = {
    'demo.json', 'source_catalog.json', 'zju_pilot.json',
    'evaluation/development.json', 'evaluation/holdout_manifest.json',
    'evaluation/vision_acceptance_example.json',
    'experiment_forms/manager_observations.csv', 'experiment_forms/public_responses.csv',
    'experiment_forms/shadow_daily_log.csv',
}
EXCLUDED_PARTS = {'runtime', 'evidence', '.git', '.venv', '__pycache__', 'node_modules', 'tools', '.env'}
EXCLUDED_STEMS = {'transfer_protected_owner', 'transfer_secrets', 'owner_transfer'}
REQUIRED = {'server.py', 'auth.py', 'secure_storage.py', 'camera_vision.py', 'monitoring.py',
            'response_plans.py', 'vision_acceptance.py', 'doctor.py', 'security_cli.py', 'instance_lock.py', 'web/draft_guard.js',
            'web/index.html', 'data/demo.json', 'requirements.txt', 'start.ps1', 'start.cmd', 'setup.ps1'}


def reject_link(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise ValueError(f'发布目录不能包含符号链接或 junction：{path.name}')


def public_file(path: Path) -> bool:
    return not any(part.casefold() in EXCLUDED_PARTS for part in path.parts) and path.stem.casefold() not in EXCLUDED_STEMS


def collect_files(root: Path) -> list[tuple[str, Path]]:
    """Walk only reviewed public subtrees; never recursively scan project root."""
    root = root.absolute()
    for directory in (root, *root.parents):
        reject_link(directory)
    found = {}
    for path in root.iterdir():
        if path.name in ROOT_FILES or (path.suffix == '.py' and public_file(Path(path.name))):
            reject_link(path)
            if path.is_file():
                found[path.name] = path
    for dirname, extensions in DIR_EXTENSIONS.items():
        directory = root / dirname
        if not directory.exists():
            continue
        reject_link(directory)
        for base, dirs, names in os.walk(directory, followlinks=False):
            base = Path(base)
            dirs[:] = [name for name in dirs if public_file(Path(name)) and not name.startswith('.')]
            for name in dirs:
                reject_link(base / name)
            for name in names:
                relative = (base / name).relative_to(root)
                if public_file(relative) and Path(name).suffix in extensions and not name.startswith('.'):
                    reject_link(base / name)
                    found[relative.as_posix()] = base / name
    data_root = root / 'data'
    if data_root.exists():
        reject_link(data_root)
    for name in sorted(DATA_FILES):
        path = data_root / name
        for parent in reversed(path.parents):
            if parent != root and root in parent.parents and parent.exists():
                reject_link(parent)
        if path.exists() or path.is_symlink():
            reject_link(path)
            if path.is_file():
                found['data/' + name] = path
    missing = sorted(REQUIRED - found.keys())
    if missing:
        raise ValueError('发布文件缺失：' + ', '.join(missing))
    return sorted(found.items())


def build_release(root: Path, output: Path) -> dict:
    entries = collect_files(root)
    output = output.absolute()
    if output.suffix.lower() != '.zip':
        raise ValueError('输出必须是 .zip 文件')
    for parent in (output.parent, *output.parent.parents):
        if parent.exists() or parent.is_symlink():
            reject_link(parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    for parent in (output.parent, *output.parent.parents):
        reject_link(parent)
    if output.exists() or output.is_symlink():
        reject_link(output)
    checksum = output.with_suffix(output.suffix + '.sha256')
    if checksum.exists() or checksum.is_symlink():
        reject_link(checksum)
    payload = []
    manifest = {'release': '0.9', 'top_level': TOP_LEVEL, 'content': 'clean-source-with-v-floodnet-adapter',
                'upstream': {'name': 'V-FloodNet', 'commit': '60c30fbac9d264d020148780504871556285f455',
                             'license_notice': 'No LICENSE file; upstream README states All rights are reserved.'},
                'excluded': ['runtime/', 'evidence/', 'tools/', '.env', '.git/', '.venv*/', 'account data',
                             'keys', 'model weights', 'datasets', 'caches', 'unfetched submodules'],
                'files': []}
    for name, path in entries:
        reject_link(path)
        content = path.read_bytes()
        if name == '.env.example':
            for line in content.decode('utf-8-sig').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    key, value = line.split('=', 1)
                    if value.strip() and not key.strip().startswith('SECURITY_'):
                        raise ValueError('.env.example 必须保持无端点、无密钥的空白模板')
        payload.append((name, content))
        manifest['files'].append({'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    handle, temp_name = tempfile.mkstemp(prefix='.release-', suffix='.zip', dir=output.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(temp_name, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in payload + [('RELEASE_MANIFEST.json', manifest_bytes)]:
                info = zipfile.ZipInfo(TOP_LEVEL + '/' + name, date_time=(2026, 10, 6, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum.write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    return {'output': str(output), 'sha256': digest, 'files': len(payload), 'bytes': output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description='打包公开源码与合成模板；不读取 runtime/evidence，不包含账户或密钥')
    parser.add_argument('--output', type=Path, default=ROOT.parent / '涝有准备_完整项目_v0.9_V-FloodNet.zip')
    args = parser.parse_args()
    try:
        result = build_release(ROOT, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, '打包失败：' + str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
