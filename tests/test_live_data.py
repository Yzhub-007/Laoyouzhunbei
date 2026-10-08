"""Synthetic fixtures verify semantics and failures; not evidence of live availability."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from engine import ValidationError
from live_data import (LiveService, finite, freshness, iso, make_reference,
                       parse_metar, parse_model)

NOW = 1790870400

def model_fixture():
    return {'latitude':26.07,'longitude':119.30,
            'current_units':{'time':'unixtime','temperature_2m':'°C','relative_humidity_2m':'%',
                             'precipitation':'mm','wind_speed_10m':'m/s'},
            'hourly_units':{'time':'unixtime','precipitation':'mm','wind_speed_10m':'m/s'},
            'current':{'time':NOW,'interval':900,'temperature_2m':27,'relative_humidity_2m':80,
                       'precipitation':None,'wind_speed_10m':2},
            'hourly':{'time':[NOW,NOW+3600,NOW+7200],'precipitation':[0,0.4,None],
                      'wind_speed_10m':[2,3,4]}}

def metar_fixture():
    return [{'icaoId':'ZSFZ','obsTime':NOW-3600,'temp':27,'lat':25.936,'lon':119.666,
             'rawOb':'METAR ZSFZ 011500Z 04007MPS 9999 -SHRA BKN015 27/24 Q1016',
             'wxString':'-SHRA','wspd':14}]

def payload(name):
    result = parse_model(model_fixture()) if name == 'model' else parse_metar(metar_fixture())
    result.update(fetched_epoch=NOW,fetched_at=iso(NOW))
    return result

class ParserTests(unittest.TestCase):
    def test_model_missing_is_not_zero(self):
        m = parse_model(model_fixture())
        self.assertIsNone(m['precipitation_mm'])
        self.assertEqual(m['interval_seconds'],900)
        self.assertEqual(m['kind'],'model_estimate')
        self.assertIsNone(m['model_run_time'])

    def test_hourly_is_preceding_hour(self):
        m = parse_model(model_fixture())
        self.assertEqual(m['hourly'][1]['window_start'],iso(NOW))
        self.assertEqual(m['hourly'][1]['time'],iso(NOW+3600))

    def test_bad_units_rejected(self):
        raw = model_fixture(); raw['current_units']['wind_speed_10m']='km/h'
        with self.assertRaises(ValueError): parse_model(raw)

    def test_wrong_location_rejected(self):
        raw = model_fixture(); raw['latitude']=30
        with self.assertRaises(ValueError): parse_model(raw)

    def test_bad_hourly_rejected(self):
        raw=model_fixture();raw['hourly']['precipitation'].pop()
        with self.assertRaises(ValueError): parse_model(raw)
        raw=model_fixture();raw['hourly']['time'][1]=NOW
        with self.assertRaises(ValueError): parse_model(raw)

    def test_invalid_quantities_null(self):
        for v in [True,float('nan'),float('inf'),-1,9999,'2']:
            self.assertIsNone(finite(v,0,150))

    def test_metar_mps_from_raw_not_rounded_json(self):
        m=parse_metar(metar_fixture())
        self.assertEqual(m['wind_speed_ms'],7)
        self.assertIsNone(m['precipitation_mm'])
        self.assertEqual(m['data_time'],'2026-10-01T23:00:00+08:00')

    def test_metar_knots_and_gust(self):
        raw=metar_fixture();raw[0]['rawOb']='METAR ZSFZ 011500Z VRB10G20KT 9999'
        m=parse_metar(raw)
        self.assertEqual(m['wind_speed_ms'],5.14)
        self.assertEqual(m['wind_gust_ms'],10.29)
        self.assertIsNone(m['wind_direction_degrees'])

    def test_other_station_rejected(self):
        raw=metar_fixture();raw[0]['icaoId']='ZBAA'
        with self.assertRaises(ValueError): parse_metar(raw)

    def test_freshness(self):
        self.assertEqual(freshness(NOW,NOW+3600,3600),'fresh')
        self.assertEqual(freshness(NOW,NOW+3601,3600),'stale')
        self.assertEqual(freshness(NOW+301,NOW,3600),'future')

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.calls=[];self.failed=False
        def fetcher(name):
            self.calls.append(name)
            return (None,'test failure') if self.failed else (payload(name),None)
        self.service=LiveService(fetcher=fetcher,clock=lambda:self.now)

    def test_rate_limit_and_forecast_filter(self):
        first=self.service.snapshot();self.service.snapshot()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(len(first['providers']['model']['hourly']),2)
        self.now+=600;self.service.snapshot()
        self.assertEqual(len(self.calls),4)

    def test_failure_preserves_but_disables_cached_data(self):
        first=self.service.snapshot();self.now+=600;self.failed=True
        second=self.service.snapshot()
        m=second['providers']['model']
        self.assertEqual(m['data_time'],first['providers']['model']['data_time'])
        self.assertEqual(m['status'],'degraded')
        self.assertFalse(m['usable'])
        with self.assertRaises(ValueError):make_reference(second)

    def test_cold_failure_never_simulates(self):
        self.failed=True;s=self.service.snapshot()
        self.assertEqual(s['providers']['model']['status'],'unavailable')
        self.assertNotIn('temperature_c',s['providers']['station'])
        self.assertEqual(s['official_alerts']['status'],'not_connected')

    def test_old_success_is_stale(self):
        self.now+=8000;s=self.service.snapshot()
        self.assertEqual(s['providers']['station']['status'],'stale')
        self.assertFalse(s['providers']['model']['usable'])

    def test_reference_cannot_assign_alert_or_hazards(self):
        w=make_reference(self.service.snapshot())
        self.assertEqual(w['hazards'],[])
        self.assertEqual(w['level'],'未说明')
        self.assertEqual(w['official_alert_status'],'not_connected')
        self.assertEqual(len(w['live_sources']),2)

    def test_corrupt_disk_cache_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'cache.json'
            path.write_text('{"model":42,"station":{"checked_epoch":1,"data":[]}}')
            service=LiveService(path,fetcher=lambda n:(payload(n),None),clock=lambda:NOW)
            self.assertTrue(service.snapshot()['providers']['model']['usable'])
            restored=LiveService(path,fetcher=lambda n:self.fail('cache unexpectedly refetched'),clock=lambda:NOW)
            self.assertTrue(restored.snapshot()['providers']['station']['usable'])

class ReceiptTests(unittest.TestCase):
    def test_immutable_receipt_and_expiry(self):
        service=LiveService(fetcher=lambda n:(payload(n),None),clock=lambda:NOW)
        weather=make_reference(service.snapshot())
        with tempfile.TemporaryDirectory() as temp, patch.object(server,'DB_PATH',Path(temp)/'test.sqlite3'), patch.object(server.time,'time',return_value=NOW):
            with self.assertRaises(ValidationError):server.verify_live_reference({'weather':weather})
            with server.database() as db:
                db.execute('INSERT INTO live_snapshots VALUES (?,?,?)',(weather['live_snapshot_id'],json.dumps(weather),iso(NOW)))
            weather['hazards']=['rain'];server.verify_live_reference({'weather':weather})
            changed=copy.deepcopy(weather);changed['level']='红色'
            with self.assertRaises(ValidationError):server.verify_live_reference({'weather':changed})
            changed=copy.deepcopy(weather);changed['live_sources']['model']['precipitation_mm']=500
            with self.assertRaises(ValidationError):server.verify_live_reference({'weather':changed})
            with patch.object(server.time,'time',return_value=NOW+8000):
                with self.assertRaises(ValidationError):server.verify_live_reference({'weather':weather})

if __name__ == '__main__':unittest.main()
