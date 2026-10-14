"""Local owner console: setup proof and encrypted configuration, never argv secrets."""
import argparse
import getpass
import re
from pathlib import Path

from auth import AuthService
from secure_storage import read_json, write_json, protect_file

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description='本机安全管理，秘密值使用隐藏输入')
    parser.add_argument('command', choices=('setup-code', 'set-secret', 'remove-secret'))
    parser.add_argument('key', nargs='?')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    args = parser.parse_args()
    args.runtime.mkdir(parents=True, exist_ok=True)
    protect_file(args.runtime)
    if args.command == 'setup-code':
        auth = AuthService(args.runtime)
        path = auth.ensure_setup_code(force=True)
        print('新的单次初始化码已写入：' + str(path.resolve()))
        print('有效20分钟，仅在本机查看并输入初始化页面；不要发送到聊天。')
        return
    if not args.key or not re.fullmatch(r'(VISION|LLM|CAMERA|MONITOR|SECURITY|ZJDATA)_[A-Z0-9_]+', args.key):
        parser.error('需要合法的配置名称，如 VISION_ENDPOINT')
    path = args.runtime / 'secrets.dpapi'
    data = read_json(path, default={})
    if args.command == 'remove-secret':
        data.pop(args.key, None)
    else:
        value = getpass.getpass('请输入 ' + args.key + '（不会显示）：')
        if not value:
            parser.error('值不能为空；删除使用 remove-secret')
        data[args.key] = value
    write_json(path, data)
    print('配置已加密保存；重启服务器生效。未打印配置值。')


if __name__ == '__main__':
    main()
