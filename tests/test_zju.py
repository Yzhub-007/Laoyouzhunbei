import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import server
from campus_agent import readiness, run_agent, fingerprint
from engine import ValidationError, candidate_pairs, model_input
from experiments import start_trial, stop_trial, finish_trial, check_generation, save_trial
from live_data import parse_model, parse_metar, ZJU_LOCATION, LiveService, make_reference, iso
from test_live_data import model_fixture, metar_fixture, NOW

BASE = json.loads((Path(__file__).resolve().parents[1]/'data/zju_pilot.json').read_text(encoding='utf-8'))


def verified_data():
    data = copy.deepcopy(BASE)
    data['site'].update(status='verified', name='测试楼宇', scope='本楼与已确认周边通道', authorization={
        'status':'confirmed','approver_role':'管理代表','reference':'synthetic-authorization-001','confirmed_at':'2026-01-01T00:00:00+08:00'})
    for i,p in enumerate(data['points']):
        p.update(name=f'测试点位{i}',source=f'合成台账{i}',status='verified',verified_by='管理代表',verified_at='2026-01-01T00:00:00+08:00')
    for i,p in enumerate(data['plans']):
        p.update(source=f'合成清单{i}',role='楼宇管理',status='verified',verified_by='管理代表',verified_at='2026-01-01T00:00:00+08:00')
    return data


class CampusTests(unittest.TestCase):
    def test_placeholder_not_shadow_ready(self):
        report=readiness(BASE)
        self.assertFalse(report['shadow_ready'])
        self.assertEqual(len(run_agent(BASE,scope_confirmed=True)['cards']),5)

    def test_shadow_rejects_unverified_and_simulation(self):
        with self.assertRaisesRegex(ValidationError,'尚未就绪'):
            run_agent(BASE,scope_confirmed=True,operation_mode='shadow')
        with self.assertRaisesRegex(ValidationError,'新近气象快照'):
            run_agent(verified_data(),scope_confirmed=True,operation_mode='shadow')

    def test_shadow_valid_data_and_live_mode(self):
        data=verified_data();data['weather'].update(mode='live_reference',live_snapshot_id='test',official_alert_status='not_connected')
        self.assertTrue(readiness(data)['shadow_ready'])
        self.assertEqual(run_agent(data,scope_confirmed=True,operation_mode='shadow')['operation_mode'],'shadow')
        # The HTTP handler separately verifies the stored immutable live receipt.

    def test_stale_placeholder_cannot_pass_by_checkbox(self):
        data=verified_data();data['points'][0]['name']='入口（采集槽位）'
        self.assertFalse(readiness(data)['shadow_ready'])

    def test_future_verification_date_not_ready(self):
        data=verified_data();data['plans'][0]['verified_at']='2099-01-01T00:00:00+08:00'
        self.assertFalse(readiness(data)['shadow_ready'])

    def test_campus_and_point_scope(self):
        data=copy.deepcopy(BASE);data['points'][0]['campus']='玉泉校区'
        self.assertNotIn(('ZJG-P01','ZJG-R01'),candidate_pairs(data))
        data['plans'][2]['point_ids']=['ZJG-P03']
        self.assertNotIn(('ZJG-P04','ZJG-R03'),candidate_pairs(data))

    def test_expired_plan_is_not_candidate(self):
        data=copy.deepcopy(BASE);data['plans'][0]['valid_until']='2026-01-01T00:00:00+08:00'
        self.assertNotIn(('ZJG-P01','ZJG-R01'),candidate_pairs(data))

    def test_model_input_excludes_authorization_identity_and_gold(self):
        data=verified_data();data['gold_answer']='secret answer';data['site']['authorization']['reference']='private record'
        packet=json.dumps(model_input(data),ensure_ascii=False)
        for word in ('verified_by','authorization','private record','gold_answer','secret answer'):
            self.assertNotIn(word,packet)

    def test_fingerprint_changes_on_input_change(self):
        other=copy.deepcopy(BASE);other['weather']['text']+='更新'
        self.assertNotEqual(fingerprint(BASE),fingerprint(other))

    def test_hangzhou_sources_and_reference(self):
        raw=model_fixture();raw.update(latitude=30.27,longitude=120.15)
        model=parse_model(raw,ZJU_LOCATION);model.update(fetched_epoch=NOW,fetched_at=iso(NOW))
        metar=metar_fixture();metar[0].update(icaoId='ZSHC',lat=30.23,lon=120.43,rawOb='METAR ZSHC 011500Z 04007MPS 9999')
        station=parse_metar(metar,'ZSHC','杭州萧山国际机场');station.update(fetched_epoch=NOW,fetched_at=iso(NOW))
        service=LiveService(location=ZJU_LOCATION,clock=lambda:NOW,fetcher=lambda name:(model if name=='model' else station,None))
        result=make_reference(service.snapshot())
        self.assertEqual(result['region'],'杭州市')
        self.assertIn('ZSHC',result['text'])
        self.assertNotIn('福州',json.dumps(result,ensure_ascii=False))


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.patch=patch.object(server,'DB_PATH',Path(self.temp.name)/'trials.sqlite3');self.patch.start()
    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def begin(self,db,condition='manual'):
        return start_trial(db,dict(participant='M01',scenario_id='A1',condition=condition,data=BASE))

    def scores(self,trial):
        return dict(trial_id=trial['id'],outcome='completed',evaluator='O01',notes='依据答案V1核对',critical_errors=0,omissions=0,unsupported=0,help_count=0)

    def test_manual_stop_then_score_and_no_overwrite(self):
        with server.database() as db:
            trial=self.begin(db)
            with self.assertRaisesRegex(ValidationError,'先停止'):finish_trial(db,self.scores(trial))
            stopped=stop_trial(db,{'trial_id':trial['id']})
            with patch('experiments.now',return_value='2099-01-01T00:00:00+00:00'):
                result=finish_trial(db,self.scores(trial))
            self.assertEqual(result['duration_seconds'],stopped['duration_seconds'])
            self.assertGreater(result['total_wall_seconds'],result['duration_seconds'])
            with self.assertRaises(ValidationError):finish_trial(db,self.scores(trial))

    def test_missing_generation_can_only_abort(self):
        with server.database() as db:
            trial=self.begin(db,'ai')
            with self.assertRaises(ValidationError):stop_trial(db,{'trial_id':trial['id']})
            payload=self.scores(trial);payload['outcome']='aborted'
            self.assertEqual(finish_trial(db,payload)['status'],'aborted')

    def test_condition_or_input_change_rejected(self):
        with server.database() as db:
            trial=self.begin(db,'rules')
            with self.assertRaises(ValidationError):check_generation(db,trial['id'],BASE,'ai')
            changed=copy.deepcopy(BASE);changed['weather']['text']+='changed'
            with self.assertRaises(ValidationError):check_generation(db,trial['id'],changed,'rules')

    def test_snapshot_at_stop_survives_later_edits(self):
        with server.database() as db:
            trial=self.begin(db,'rules');trial['run_id']='run1';save_trial(db,trial)
            result={'revision':0,'audit':[],'cards':[{'action':'original'}]}
            db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',('run1','2026-01-01','rules','{}',json.dumps(result)))
            stop_trial(db,{'trial_id':trial['id']})
            db.execute('UPDATE runs SET output_json=? WHERE id=?',(json.dumps({'revision':9,'cards':[]}), 'run1'))
            done=finish_trial(db,self.scores(trial))
            self.assertEqual(done['result_at_finish']['cards'][0]['action'],'original')
            self.assertEqual(done['run_revision_at_finish'],0)

    def test_anonymous_identifier_required(self):
        with server.database() as db:
            with self.assertRaises(ValidationError):start_trial(db,dict(participant='张同学',scenario_id='A1',condition='manual',data=BASE))


if __name__=='__main__':unittest.main()
