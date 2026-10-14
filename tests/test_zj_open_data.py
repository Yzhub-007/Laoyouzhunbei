import json
import unittest

from integrations.zj_open_data.alerts import (
    GATEWAY_URL, INTERFACES, SIGN_URL, ZhejiangAlertArchiveClient,
    ZhejiangDataError, normalize_alert, parse_platform_response,
)


ROW = {
    'unit_name':'永嘉县气象台','yj_ptime':'2021-06-14 15:17:00','unit_long':'120.682144',
    'yj_id':'33032441600000_20210614151834','yj_addr':'浙江省_温州市_永嘉县','yj_grade':'3',
    'unit_lat':'28.150591','yj_validtime':'2021-06-14 21:17:00',
    'yj_content':'永嘉县气象台发布雷电黄色预警信号。','yj_eventype':'11B14',
    'yj_ch':'发布雷电黄色预警[Ⅲ级/较重]','yj_sendtime':'2021-06-14 15:17:00',
}


def wrapped(data):
    return json.dumps({'code':200,'msg':'','subCode':None,'subMsg':None,
                       'data':json.dumps({'msg':'成功','data':data,'status':1},ensure_ascii=False)},
                      ensure_ascii=False).encode()


class ParserTests(unittest.TestCase):
    def test_double_encoded_platform_response(self):
        self.assertEqual(parse_platform_response(wrapped([ROW]))[0]['yj_grade'],'3')

    def test_normalizes_fields_and_beijing_time(self):
        alert=normalize_alert(ROW)
        self.assertEqual(alert['alert_id'],'33032441600000_20210614151834')
        self.assertEqual(alert['published_at'],'2021-06-14T15:17:00+08:00')
        self.assertEqual(alert['region_path'],['浙江省','温州市','永嘉县'])
        self.assertEqual(alert['longitude'],120.682144)
        self.assertEqual(alert['source_kind'],'official_monthly_archive')

    def test_invalid_status_and_coordinates_fail_closed(self):
        with self.assertRaises(ZhejiangDataError):
            parse_platform_response(json.dumps({'code':200,'data':'{"status":0,"data":[]}'}))
        row=dict(ROW,unit_lat='999')
        with self.assertRaises(ZhejiangDataError):normalize_alert(row)


class ClientTests(unittest.TestCase):
    def test_sign_then_page_request_without_app_secret(self):
        calls=[]
        def transport(url,fields):
            calls.append((url,fields))
            return 'A'*128 if url==SIGN_URL else wrapped([ROW,ROW])
        client=ZhejiangAlertArchiveClient('test-user-secret',transport=transport,clock=lambda:1)
        alerts=client.fetch_page(2,200)
        self.assertEqual(len(alerts),1)
        self.assertEqual(calls[0][0],SIGN_URL)
        self.assertEqual(calls[1][0],GATEWAY_URL)
        self.assertEqual(calls[0][1]['interface_id'],INTERFACES['page'])
        content=json.loads(calls[0][1]['biz_content'])
        self.assertEqual(content['pageNum'],2)
        self.assertEqual(content['pageSize'],200)
        self.assertNotIn('appSecret',content)
        self.assertEqual(calls[1][1]['sign'],'A'*128)

    def test_page_size_is_bounded(self):
        client=ZhejiangAlertArchiveClient('test-user-secret',transport=lambda *_:b'')
        with self.assertRaises(ZhejiangDataError):client.fetch_page(1,201)


if __name__ == '__main__':unittest.main()
