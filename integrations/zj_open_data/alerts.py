"""Import the Zhejiang meteorological warning archive without exposing secrets.

The selected resource is updated monthly and is therefore an official archive,
not a source for deciding whether a warning is active right now.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from secure_storage import read_json, write_json

SIGN_URL = 'https://data.zjzwfw.gov.cn/jimp/sign/createsign.do'
GATEWAY_URL = 'https://data.zjzwfw.gov.cn/interface/gateway.do'
APP_ID = 'shengji'
INTERFACES = {
    'count': 'zj194262count',
    'page': 'zj194262page',
    'date': 'zj194262date',
}
BEIJING = timezone(timedelta(hours=8))
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ZhejiangDataError(ValueError):
    """The upstream response cannot be safely used."""


def _text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ZhejiangDataError(field + ' 缺失或格式异常。')
    return value.strip()


def _time(value, field):
    value = _text(value, field, 32)
    try:
        parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=BEIJING)
    except ValueError as exc:
        raise ZhejiangDataError(field + ' 不是预期的北京时间。') from exc
    return parsed.isoformat(timespec='seconds')


def _coordinate(value, field, low, high):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ZhejiangDataError(field + ' 不是有效坐标。') from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ZhejiangDataError(field + ' 超出有效范围。')
    return result


def parse_platform_response(raw):
    """Decode the platform's outer JSON and its JSON-encoded data field."""
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    try:
        outer = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ZhejiangDataError('浙江开放数据平台返回的不是有效JSON。') from exc
    if not isinstance(outer, dict) or outer.get('code') != 200:
        raise ZhejiangDataError('浙江开放数据平台请求失败。')
    inner = outer.get('data')
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError as exc:
            raise ZhejiangDataError('平台data字段不是有效JSON。') from exc
    if not isinstance(inner, dict) or inner.get('status') != 1:
        raise ZhejiangDataError('浙江开放数据接口返回失败状态。')
    return inner.get('data')


def parse_sign(raw):
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    candidate = raw.strip() if isinstance(raw, str) else ''
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        value = candidate
    if isinstance(value, dict):
        value = value.get('sign', value.get('data'))
        if isinstance(value, str) and value.startswith('{'):
            try:
                nested = json.loads(value)
                value = nested.get('sign', nested.get('data')) if isinstance(nested, dict) else value
            except json.JSONDecodeError:
                pass
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9+/=_-]{16,1024}', value.strip()):
        raise ZhejiangDataError('验签接口没有返回有效sign。')
    return value.strip()


def normalize_alert(row):
    if not isinstance(row, dict):
        raise ZhejiangDataError('预警记录不是对象。')
    published_at = _time(row.get('yj_ptime'), 'yj_ptime')
    valid_until = _time(row.get('yj_validtime'), 'yj_validtime')
    if valid_until < published_at:
        raise ZhejiangDataError('预警有效期早于发布时间。')
    address = _text(row.get('yj_addr'), 'yj_addr', 300)
    return {
        'alert_id': _text(row.get('yj_id'), 'yj_id', 120),
        'publisher': _text(row.get('unit_name'), 'unit_name', 200),
        'published_at': published_at,
        'sent_at': _time(row.get('yj_sendtime'), 'yj_sendtime'),
        'valid_until': valid_until,
        'region_path': address.split('_'),
        'region_text': address.replace('_', ' / '),
        'grade': _text(row.get('yj_grade'), 'yj_grade', 20),
        'event_code': _text(row.get('yj_eventype'), 'yj_eventype', 50),
        'title': _text(row.get('yj_ch'), 'yj_ch', 300),
        'content': _text(row.get('yj_content'), 'yj_content', 10000),
        'longitude': _coordinate(row.get('unit_long'), 'unit_long', -180, 180),
        'latitude': _coordinate(row.get('unit_lat'), 'unit_lat', -90, 90),
        'source_id': 'ZJ-CMA-ALERT-ARCHIVE',
        'source_kind': 'official_monthly_archive',
    }


def _post_form(url, fields):
    request = Request(url, data=urlencode(fields).encode('utf-8'), headers={
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8',
        'User-Agent': 'Laoyouzhunbei/0.9 ZhejiangOpenDataConnector',
    })
    with urlopen(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ZhejiangDataError('浙江开放数据平台响应超过2MiB限制。')
        return raw


class ZhejiangAlertArchiveClient:
    def __init__(self, user_secret, app_secret='', *, transport=_post_form, clock=time.time):
        self.user_secret = _text(user_secret, 'userSecret', 512)
        self.app_secret = app_secret.strip() if isinstance(app_secret, str) else ''
        if len(self.app_secret) > 512:
            raise ZhejiangDataError('appSecret格式异常。')
        self.transport = transport
        self.clock = clock

    def _request(self, interface_id, page_num=1, page_size=200):
        if not isinstance(page_num, int) or isinstance(page_num, bool) or page_num < 1:
            raise ZhejiangDataError('pageNum必须是正整数。')
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 200:
            raise ZhejiangDataError('pageSize必须在1到200之间。')
        content = {'userSecret': self.user_secret, 'pageNum': page_num, 'pageSize': page_size}
        if self.app_secret:
            content['appSecret'] = self.app_secret
        fields = {
            'app_id': APP_ID,
            'version': '1.0.0',
            'charset': 'utf-8',
            'timestamp': str(int(self.clock() * 1000)),
            'origin': '0',
            'interface_id': interface_id,
            'biz_content': json.dumps(content, ensure_ascii=False, separators=(',', ':')),
        }
        sign = parse_sign(self.transport(SIGN_URL, fields))
        return parse_platform_response(self.transport(GATEWAY_URL, {**fields, 'sign': sign}))

    def fetch_page(self, page_num=1, page_size=200):
        rows = self._request(INTERFACES['page'], page_num, page_size)
        if not isinstance(rows, list):
            raise ZhejiangDataError('分页接口data不是数组。')
        alerts, seen = [], set()
        for row in rows:
            alert = normalize_alert(row)
            if alert['alert_id'] not in seen:
                seen.add(alert['alert_id'])
                alerts.append(alert)
        return alerts

    def count(self):
        value = self._request(INTERFACES['count'], 1, 1)
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ZhejiangDataError('总条数接口没有返回整数。') from exc
        if count < 0:
            raise ZhejiangDataError('总条数不能为负数。')
        return count

    def updated_at(self):
        return self._request(INTERFACES['date'], 1, 1)


def main():
    parser = argparse.ArgumentParser(description='安全下载浙江气象灾害预警信号历史归档')
    parser.add_argument('--runtime', type=Path, default=Path(__file__).resolve().parents[2] / 'runtime')
    parser.add_argument('--page', type=int, default=1)
    parser.add_argument('--page-size', type=int, default=200)
    args = parser.parse_args()
    secrets = read_json(args.runtime / 'secrets.dpapi', default={})
    user_secret = secrets.get('ZJDATA_USER_SECRET')
    if not user_secret:
        parser.error('缺少ZJDATA_USER_SECRET；请用security_cli.py set-secret安全录入。')
    client = ZhejiangAlertArchiveClient(user_secret, secrets.get('ZJDATA_APP_SECRET', ''))
    try:
        alerts = client.fetch_page(args.page, args.page_size)
    except (HTTPError, URLError, TimeoutError, OSError, ZhejiangDataError) as exc:
        parser.exit(1, '获取失败：' + str(exc) + '\n')
    fetched_at = datetime.now(BEIJING).isoformat(timespec='seconds')
    output = args.runtime / 'zj_alert_archive.dpapi'
    write_json(output, {'fetched_at': fetched_at, 'page': args.page,
                        'page_size': args.page_size, 'alerts': alerts})
    print(f'已加密保存{len(alerts)}条官方历史预警记录：{output.resolve()}')
    print('该资源按月更新，不得作为当前实时预警或自动处置依据。')


if __name__ == '__main__':
    main()
