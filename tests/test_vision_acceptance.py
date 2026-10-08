import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vision_acceptance import AcceptanceError, MAX_BYTES, MAX_RECORDS, evaluate, load_document, main

ROOT = Path(__file__).resolve().parents[1]


def dataset():
    rows = []
    for event in ('E001', 'E002'):
        for label, prediction in (('water', 'water'), ('clear', 'clear'), ('unusable', 'unknown')):
            rows.append({'point_id': 'P001', 'event_id': event, 'frame_id': event + '_' + label,
                         'split': 'test', 'label': label, 'prediction': prediction})
    return {'schema_version': 1, 'dataset_kind': 'field', 'independent_events_confirmed': True, 'records': rows}


def thresholds():
    # Deliberately tiny test fixture values, never a field-acceptance recommendation.
    return {'min_water_recall': 1.0, 'min_water_precision': 1.0,
            'max_clear_false_positive_rate': 0.0, 'max_usable_unknown_rate': 0.0,
            'max_unusable_definite_rate': 0.0, 'min_test_events': 2,
            'min_water_samples': 1, 'min_clear_samples': 1, 'min_unusable_samples': 1}


class VisionAcceptanceTests(unittest.TestCase):
    def test_unconfigured_thresholds_only_produce_statistics(self):
        report = evaluate(dataset())
        self.assertEqual(report['status'], 'not_evaluated')
        self.assertIsNone(report['thresholds'])
        self.assertEqual(report['overall']['water_detection_recall'], 1)

    def test_complete_field_fixture_passes_only_configured_thresholds(self):
        report = evaluate(dataset(), thresholds())
        self.assertEqual(report['status'], 'passed_configured_thresholds')
        self.assertEqual(report['points'][0]['status'], 'passed_configured_thresholds')
        self.assertEqual(report['overall']['event_count'], 2)

    def test_unknown_water_is_undetected_and_never_clear(self):
        data = dataset()
        data['records'][0]['prediction'] = 'unknown'
        data['records'][3]['prediction'] = 'clear'
        result = evaluate(data, thresholds())
        metrics = result['overall']
        self.assertEqual(metrics['water_detection_recall'], 0)
        self.assertEqual(metrics['explicit_water_misses'], 1)
        self.assertEqual(metrics['unknown_water_undetected'], 1)
        self.assertEqual(metrics['total_water_undetected'], 2)
        self.assertEqual(metrics['correct_clear_count'], 2)
        self.assertEqual(metrics['usable_unknown_rate'], 1 / 4)
        self.assertIsNone(metrics['water_precision'])
        self.assertNotEqual(result['status'], 'passed_configured_thresholds')

    def test_zero_denominators_are_null(self):
        data = dataset()
        data['records'] = []
        report = evaluate(data, thresholds())
        self.assertEqual(report['status'], 'insufficient')
        for key in ('water_detection_recall', 'water_precision', 'unknown_rate',
                    'clear_false_positive_rate', 'water_event_detection_recall', 'unusable_definite_rate'):
            self.assertIsNone(report['overall'][key])

    def test_no_water_or_clear_cannot_pass(self):
        for missing in ('water', 'clear', 'unusable'):
            with self.subTest(missing=missing):
                data = dataset()
                data['records'] = [r for r in data['records'] if r['label'] != missing]
                self.assertEqual(evaluate(data, thresholds())['status'], 'insufficient')

    def test_events_require_independent_collection_confirmation(self):
        data = dataset()
        data['independent_events_confirmed'] = False
        self.assertEqual(evaluate(data, thresholds())['status'], 'insufficient')

    def test_many_neighbouring_frames_do_not_create_independent_events(self):
        data = dataset()
        for row in data['records']:
            row['event_id'] = 'SAME_EVENT'
        report = evaluate(data, thresholds())
        self.assertEqual(report['overall']['event_count'], 1)
        self.assertEqual(report['status'], 'insufficient')

    def test_same_event_across_splits_rejected_even_at_different_points(self):
        for split in ('train', 'validation'):
            with self.subTest(split=split):
                data = dataset()
                extra = dict(data['records'][0], frame_id='NEW_FRAME', point_id='OTHER_POINT', split=split)
                data['records'].append(extra)
                with self.assertRaisesRegex(AcceptanceError, '事件跨 split'):
                    evaluate(data)

    def test_same_frame_rejected_with_or_without_cross_split(self):
        for split in ('test', 'validation', 'train'):
            data = dataset()
            data['records'].append(dict(data['records'][0], event_id='NEW_EVENT', split=split))
            with self.assertRaisesRegex(AcceptanceError, '重复 frame_id'):
                evaluate(data)

    def test_non_test_results_excluded_from_metrics(self):
        data = dataset()
        for split in ('train', 'validation'):
            data['records'].append(dict(data['records'][0], event_id=split, frame_id=split, split=split, prediction='clear'))
        report = evaluate(data, thresholds())
        self.assertEqual(report['split_counts'], {'train': 1, 'validation': 1, 'test': 6})
        self.assertEqual(report['overall']['sample_count'], 6)
        self.assertEqual(report['overall']['water_detection_recall'], 1)

    def test_false_alarms_and_unusable_definite_decisions_visible(self):
        data = dataset()
        data['records'][1]['prediction'] = 'water'
        data['records'][2]['prediction'] = 'water'
        data['records'][5]['prediction'] = 'clear'
        report = evaluate(data, thresholds())
        metrics = report['overall']
        self.assertEqual(metrics['false_positive_count'], 1)
        self.assertEqual(metrics['false_alarm_event_count'], 1)
        self.assertEqual(metrics['water_precision'], 2 / 3)
        self.assertEqual(metrics['unusable_definite_count'], 2)
        self.assertEqual(metrics['unusable_predicted_water_count'], 1)
        self.assertEqual(metrics['unusable_predicted_clear_count'], 1)
        self.assertEqual(report['status'], 'failed')

    def test_event_detection_does_not_inflate_with_frame_count(self):
        data = dataset()
        data['records'].append(dict(data['records'][0], frame_id='ANOTHER_WATER'))
        report = evaluate(data)
        self.assertEqual(report['overall']['water_samples'], 3)
        self.assertEqual(report['overall']['detected_water_event_count'], 2)
        self.assertEqual(len(report['events']), 2)

    def test_underrepresented_point_prevents_global_acceptance(self):
        data = dataset()
        data['records'].append(dict(data['records'][0], point_id='P002', frame_id='P002_ONLY'))
        report = evaluate(data, thresholds())
        self.assertEqual(report['status'], 'insufficient')
        self.assertIn('one_or_more_points_insufficient', report['reasons'])

    def test_report_never_copies_source_ids_paths_names_or_confidence(self):
        data = dataset()
        data['metadata'] = {'owner': 'Private owner', 'path': 'C:/secret/image.png'}
        data['records'][0].update(image_path='C:/secret/image.png', notes='Private owner', confidence=0.999)
        report_text = json.dumps(evaluate(data))
        for secret in ('P001', 'E001', 'C:/secret/image.png', 'Private owner', '0.999'):
            self.assertNotIn(secret, report_text)
        self.assertIn('point_001', report_text)
        self.assertIn('event_001', report_text)

    def test_confidence_is_not_used_as_accuracy(self):
        data = dataset()
        altered = copy.deepcopy(data)
        for row in altered['records']:
            row['confidence'] = 0.001
        self.assertEqual(evaluate(data)['overall'], evaluate(altered)['overall'])

    def test_synthetic_data_cannot_receive_field_acceptance(self):
        data = dataset()
        data['dataset_kind'] = 'synthetic'
        report = evaluate(data, thresholds())
        self.assertEqual(report['status'], 'not_evaluated')
        self.assertIn('synthetic_data_is_not_field_acceptance', report['reasons'])
        example = load_document(ROOT / 'data/evaluation/vision_acceptance_example.json')
        self.assertEqual(evaluate(example, thresholds())['status'], 'not_evaluated')

    def test_invalid_booleans_nonfinite_confidence_and_missing_fields(self):
        for value in (True, False, float('nan'), float('inf'), -0.1, 1.1, '0.8', 10 ** 1000):
            with self.subTest(value_type=type(value).__name__):
                data = dataset()
                data['records'][0]['confidence'] = value
                with self.assertRaises(AcceptanceError):
                    evaluate(data)
        for field in ('point_id', 'event_id', 'frame_id', 'split', 'label', 'prediction'):
            data = dataset()
            del data['records'][0][field]
            with self.assertRaises(AcceptanceError):
                evaluate(data)
        data = dataset()
        data['schema_version'] = True
        with self.assertRaises(AcceptanceError):
            evaluate(data)
        data = dataset()
        data['independent_events_confirmed'] = 'true'
        with self.assertRaises(AcceptanceError):
            evaluate(data)

    def test_invalid_and_incomplete_thresholds_rejected(self):
        for key, value in (('min_test_events', True), ('min_test_events', 1),
                           ('min_water_recall', float('nan')), ('min_water_recall', True),
                           ('min_water_recall', 1.2), ('min_water_samples', 1.5)):
            limits = thresholds()
            limits[key] = value
            with self.assertRaises(AcceptanceError):
                evaluate(dataset(), limits)
        with self.assertRaises(AcceptanceError):
            evaluate(dataset(), {'min_water_recall': 0.8})

    def test_oversized_record_list_rejected(self):
        data = dataset()
        data['records'] = [data['records'][0]] * (MAX_RECORDS + 1)
        with self.assertRaises(AcceptanceError):
            evaluate(data)

    def test_file_loader_rejects_duplicate_fields_nan_and_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.json'
            for text in ('{"records": [], "records": []}', '{"confidence": NaN}', '[1,2]', '{broken',
                         '{"oversized_integer": ' + '1' * 5000 + '}'):
                source.write_text(text, encoding='utf-8')
                with self.assertRaises(AcceptanceError):
                    load_document(source)
            source.write_bytes(b' ' * (MAX_BYTES + 1))
            with self.assertRaises(AcceptanceError):
                load_document(source)

    def test_cli_writes_statistics_without_overwriting_existing_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / 'input.json', Path(temporary) / 'output.json'
            source.write_text(json.dumps(dataset()), encoding='utf-8')
            self.assertEqual(main(['--input', str(source), '--output', str(output)]), 1)
            original = output.read_bytes()
            self.assertEqual(json.loads(original)['status'], 'not_evaluated')
            with self.assertRaises(SystemExit) as failed:
                main(['--input', str(source), '--output', str(output)])
            self.assertEqual(failed.exception.code, 2)
            self.assertEqual(original, output.read_bytes())


if __name__ == '__main__':
    unittest.main()
