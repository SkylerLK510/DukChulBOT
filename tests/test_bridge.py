"""Offline bridge checks: no provider or command judge calls."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bridge
import council


class BridgeTest(unittest.TestCase):
    def request(self):
        return {'operation': 'prepare', 'brief': 'Choose a design.\n\nHard constraints:\n- Keep data local.\n',
                'proposal': 'Use a local SQLite file; verify recovery after a process crash.',
                'rubric': str(council.DEFAULT_RUBRIC), 'samples': 2}

    def replies(self, req):
        rubric = council.load_rubric(req['rubric'])
        questions = council.build_questions(rubric, council.parse_constraints(req['brief']))
        replies = []
        for n in range(req['samples']):
            _, keys = council.judge_prompt(req['brief'], req['proposal'], questions, n)
            replies.append(json.dumps({key: max(levels, key=levels.get) if levels else qid.startswith('constraint_fit.')
                                       for key, (qid, levels) in keys.items()}))
        return replies

    def test_prepare_and_score_use_existing_parser_and_aggregation_without_execution(self):
        req = self.request()
        ready = bridge.exchange(req)
        self.assertEqual(len(ready['requests']), 2)
        replies = self.replies(req)
        with mock.patch.object(council, 'run_isolated', side_effect=AssertionError('no subprocess')):
            scored = bridge.exchange({**req, 'operation': 'score', 'fingerprint': ready['fingerprint'],
                                      'replies': replies, 'model': 'offline-fixture'})['report']
        self.assertTrue(scored['complete'])
        self.assertEqual(scored['decision']['action'], 'recommend')
        self.assertEqual(scored['judge_config']['bridge_fingerprint'], ready['fingerprint'])
        self.assertEqual(scored['judged'][0]['attempts'], 2)
        self.assertEqual(scored['mandatory_constraints'], [1])

    def test_changed_input_or_wrong_reply_count_is_refused(self):
        req = self.request()
        ready = bridge.exchange(req)
        score = {**req, 'operation': 'score', 'fingerprint': ready['fingerprint'], 'replies': self.replies(req)}
        for change in ({'proposal': 'changed'}, {'fingerprint': 'wrong'}, {'replies': []}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                bridge.exchange({**score, **change})

    def test_invalid_judge_reply_does_not_become_a_report(self):
        req = self.request()
        ready = bridge.exchange(req)
        with self.assertRaises(council.JudgeError):
            bridge.exchange({**req, 'operation': 'score', 'fingerprint': ready['fingerprint'],
                             'replies': ['{}', '{}']})

    def test_sample_and_constraint_validation_and_new_rubrics(self):
        for bad in (True, 1, 26, 2.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bridge.exchange({**self.request(), 'samples': bad})
        with self.assertRaises(ValueError):
            bridge.exchange({**self.request(), 'brief': 'No explicit constraints'})
        for name in ('artifact_design', 'code_change'):
            req = {**self.request(), 'rubric': str(Path(council.__file__).parent / 'rubrics' / f'{name}.json')}
            self.assertEqual(len(bridge.exchange(req)['requests']), 2)


if __name__ == '__main__':
    unittest.main()
