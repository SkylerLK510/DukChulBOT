"""No network: the TypeSafe API and Ollama are replaced by fake openers."""
import http.client
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import council as c

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / 'examples' / 'coordinator-host'


BRIEF = """Decide where the thing runs.

Hard constraints:
- Nothing may depend on SSH.
- Checkpoints must end up on permanent storage.
"""


def rubric():
    return c.load_rubric(c.DEFAULT_RUBRIC)


def score_rubric():
    """The default rubric with per-constraint dimensions turned back into plain Scores,
    so the chairman tests can drive every dimension with one number."""
    r = rubric()
    for d in r['dimensions']:
        if d.pop('per_constraint', None) is not None:
            d['instructions'] = f"How well does `proposal` do on {d['id']}?"
            d['levels'] = ['poor', 'fair', 'good', 'excellent']
    return r


TOPS = {d['id']: len(d['levels']) - 1 for d in score_rubric()['dimensions']}


def score_answer(value, top, confidence=0.9):
    """A Score answer whose probabilities match its expected value, as Jev's do."""
    low = min(int(value), top - 1)
    probs = {str(i): 0.0 for i in range(top + 1)}
    probs[str(low)] += 1 - (value - low)
    probs[str(low + 1)] += value - low
    return {'type': 'score', 'score': value, 'confidence': confidence, 'probabilities': probs}


def answers(levels, vetoes=None, confidence=0.9, addressed=None):
    """levels: {dimension_id: score}, capped at each dimension's top level; vetoes:
    {veto_id: probability}; addressed: {question_id: probability} for per-constraint questions."""
    out = {k: score_answer(min(v, TOPS[k]), TOPS[k], confidence) for k, v in levels.items()}
    for v in rubric().get('vetoes', []):
        out[v['id']] = {'type': 'noul', 'noul': (vetoes or {}).get(v['id'], 0.02)}
    for qid, p in (addressed or {}).items():
        out[qid] = {'type': 'noul', 'noul': p}
    return out


def flat(score, **kw):
    return answers({d['id']: score for d in score_rubric()['dimensions']}, **kw)


class ScriptedJudge:
    name = 'scripted'

    def __init__(self, by_text):
        self.by_text, self.seen = by_text, []

    def judge(self, brief, proposal, rub):
        self.seen.append(proposal)
        result = self.by_text[proposal]
        if isinstance(result, Exception):
            raise result
        return {'answers': result, 'model': 'fake', 'usage': None}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None, body=b'{}'):
    return urllib.error.HTTPError('u', code, 'x', headers or {}, io.BytesIO(body))


def scripted_opener(replies, calls=None):
    """Returns each reply in turn, raising the ones that are exceptions."""
    def opener(request, timeout):
        if calls is not None:
            calls.append(timeout)
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply
    return opener


class FakeClock:
    """time.monotonic and time.sleep that share one fake timeline."""
    def __init__(self):
        self.now, self.naps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.naps.append(seconds)
        self.now += seconds


class RubricTests(unittest.TestCase):
    def test_default_rubric_is_valid_and_matches_the_api_contract(self):
        questions = c.build_questions(rubric(), ['first constraint', 'second constraint'])
        for d in rubric()['dimensions']:
            if 'per_constraint' in d:
                for i, text in enumerate(['first constraint', 'second constraint'], 1):
                    q = questions[f"{d['id']}.{i}"]
                    self.assertEqual(q['type'], 'noul')
                    self.assertEqual(q['instructions']['constraint'], text)
                    self.assertIn('`proposal`', q['instructions']['question'])
                    self.assertIn('`constraint`', q['instructions']['question'])
                    self.assertEqual(set(q['criteria']), {'true', 'false'})
                self.assertNotIn(d['id'], questions)
                continue
            q = questions[d['id']]
            self.assertEqual(q['type'], 'score')
            self.assertIsInstance(q['criteria'], list)          # Score: ordered array of levels
            self.assertTrue(2 <= len(q['criteria']) <= 10)
            self.assertIn('`proposal`', q['instructions'])       # state paths are backticked
        for v in rubric()['vetoes']:
            self.assertEqual(questions[v['id']]['type'], 'noul')
            self.assertEqual(set(questions[v['id']]['criteria']), {'true', 'false'})
        self.assertAlmostEqual(sum(d['weight'] for d in rubric()['dimensions']), 1.0)

    def test_request_shape(self):
        req = c.build_request('the brief', 'the proposal', rubric())
        self.assertEqual(set(req), {'state', 'model', 'questions'})
        self.assertEqual(req['state'], {'decision_brief': 'the brief', 'proposal': 'the proposal'})
        self.assertEqual(req['model'], 'jev-latest')

    def test_bad_rubrics_are_rejected(self):
        good = json.loads(c.DEFAULT_RUBRIC.read_text())
        for mutate in (lambda r: r.update(dimensions=[]),
                       lambda r: r['dimensions'][0].update(levels=['only one']),
                       lambda r: r['dimensions'][0].update(weight=0),
                       lambda r: r['dimensions'][1].update(id=r['dimensions'][0]['id']),
                       lambda r: r['vetoes'][0].update(threshold=0),
                       lambda r: r['vetoes'][0].update(id='has.dot'),
                       lambda r: r['dimensions'][0].update(per_constraint={'question': 'Is it good?'}),
                       lambda r: r['dimensions'][0].update(levels=['a', 'b']),
                       lambda r: r['dimensions'][1].update(levels=['only one']),
                       lambda r: r['dimensions'][1].update(levels=[str(i) for i in range(11)]),
                       lambda r: r['dimensions'][1].pop('instructions'),
                       lambda r: r['dimensions'][1].update(weight=True),
                       lambda r: r['dimensions'][1].update(weight='0.2'),
                       lambda r: r['vetoes'][0].update(threshold=1.5),
                       lambda r: r['vetoes'][0].update(threshold=True),
                       lambda r: r.update(decision=[]),
                       lambda r: r['decision'].update(min_margin=-0.1),
                       lambda r: r['decision'].update(max_flip=2),
                       lambda r: r['decision'].update(allow_missing_confidence='yes'),
                       lambda r: r.update(dimensions={'not': 'a list'})):
            broken = json.loads(json.dumps(good)); mutate(broken)
            with mock.patch.object(Path, 'read_text', return_value=json.dumps(broken)):
                with self.assertRaises(ValueError, msg=json.dumps(broken)[:80]):
                    c.load_rubric('x')

    def test_nan_and_infinite_values_never_load(self):
        text = c.DEFAULT_RUBRIC.read_text()
        for bad in (text.replace('"weight": 0.2,', '"weight": NaN,', 1),
                    text.replace('"weight": 0.2,', '"weight": Infinity,', 1),
                    text.replace('"threshold": 0.7,', '"threshold": NaN,', 1)):
            self.assertNotEqual(bad, text)
            with mock.patch.object(Path, 'read_text', return_value=bad), self.assertRaises(ValueError):
                c.load_rubric('x')


class PerConstraintTests(unittest.TestCase):
    def test_parses_the_example_brief(self):
        found = c.parse_constraints((EXAMPLE / 'brief.md').read_text())
        self.assertEqual(len(found), 7)
        self.assertTrue(found[0].startswith('Remote access to the desktop is NOT configured'))
        self.assertTrue(found[-1].startswith('Nothing may be assumed to behave on Windows'))

    def test_heading_numbering_continuations_and_the_end_of_the_list(self):
        brief = ('Intro.\n\n## Hard Constraints\n\n1. Stay under 2 GB of\n   memory at peak.\n\n'
                 '2) No new services.\n* Ship by Friday.\n\nAfterwards we can talk.\n- not a constraint\n')
        self.assertEqual(c.parse_constraints(brief),
                         ['Stay under 2 GB of memory at peak.', 'No new services.', 'Ship by Friday.'])
        self.assertEqual(c.parse_constraints('No list here.\n- a bullet in the wrong place'), [])

    def test_lists_are_read_the_way_markdown_reads_them(self):
        cases = {
            # a wrapped line that is not indented still belongs to its bullet
            'Hard constraints:\n- Nothing may depend on SSH or remote desktop\nexisting on the desktop.\n- Keep checkpoints.\n':
                ['Nothing may depend on SSH or remote desktop existing on the desktop.', 'Keep checkpoints.'],
            # nested bullets are part of the constraint above, not new constraints
            'Hard constraints:\n- Storage:\n  - checkpoints on the desktop\n  - fsync before rename\n- No SSH.\n':
                ['Storage: checkpoints on the desktop; fsync before rename', 'No SSH.'],
            'Hard constraints:\r\n- a\r\n- b\r\n': ['a', 'b'],
            'Hard constraints:\nThese are firm.\n- a\n': ['a'],
            'Hard constraints\n----------------\n- a\n': ['a'],
            'Hard constraints:\n- a\n- b\n\n* * *\n\n- c\n': ['a', 'b'],
            'Hard constraints:\n- a\n- - -\n- c\n': ['a'],
            'Hard constraints:\n- a\n## Next section\n- c\n': ['a'],
            'Hard constraints:\n- a\n\nMore context.\n\nHard constraints:\n- b\n': ['a', 'b'],
            # nothing inside a code fence counts, so a brief can quote an example
            'Example:\n```\nHard constraints:\n- fake one\n```\n\nHard constraints:\n- real\n': ['real'],
            'Example:\n~~~~\nHard constraints:\n- fake one\n```\n~~~~\n': [],
            # an indented fence belongs to the bullet above and the list goes on
            'Hard constraints:\n- a\n  ```\n  - not a constraint\n  ```\n- b\n': ['a', 'b'],
            # an unindented fence ends the list, like a heading
            'Hard constraints:\n- a\n```\n- code\n```\n- c\n': ['a'],
            # U+2028 is not a Markdown line ending, so the text after it is not a heading
            'Hard constraints:\n- Use the path\u2028# not a heading\n- b\n': ['Use the path # not a heading', 'b'],
        }
        for brief, expected in cases.items():
            self.assertEqual(c.parse_constraints(brief), expected, brief)
        for heading in ('**Hard constraints:**', '**Hard constraints**:', '### Hard Constraints ###',
                        'Hard constraints (non-negotiable):', 'Hard constraint:', '   hard constraints'):
            self.assertEqual(c.parse_constraints(f'{heading}\n- a\n- b\n'), ['a', 'b'], heading)
        self.assertEqual(c.parse_constraints('Hard constraints make this tricky because of X.\n- a\n'), [])

    def test_mean_probability_and_the_weakest_constraint_is_named(self):
        rub = rubric()
        a = answers({d['id']: 3 for d in rub['dimensions'] if 'levels' in d},
                    addressed={'constraint_fit.1': 0.9, 'constraint_fit.2': 0.5})
        b = answers({d['id']: 1 for d in rub['dimensions'] if 'levels' in d},
                    addressed={'constraint_fit.1': 0.1, 'constraint_fit.2': 0.1})
        items = c.anonymise([('a.md', 'a'), ('b.md', 'b')], seed=0)
        report = c.build_report(BRIEF, c.judge_all(BRIEF, items, rub, ScriptedJudge({'a': a, 'b': b})), rub, 'scripted', 0)
        self.assertEqual(report['constraints'], ['Nothing may depend on SSH.', 'Checkpoints must end up on permanent storage.'])
        fit = next(s for s in report['scored'] if s['source'] == 'a.md')['dimensions']['constraint_fit']
        self.assertEqual((fit['normalised'], fit['confidence']), (0.7, 0.0))
        d = report['decision']
        self.assertEqual((d['action'], d['recommended_source']), ('escalate', 'a.md'))
        self.assertTrue(any('unclear whether it addresses "Checkpoints must end up on permanent storage." (p=0.50)' in r
                            for r in d['reasons']), d['reasons'])
        text = c.render(report)
        self.assertIn('constraint_fit: probability that each proposal addresses the constraint', text)
        self.assertIn('2. Checkpoints must end up on permanent storage.', text)
        again = c.rescore(report, rub)
        self.assertEqual(again['decision'], d)

    def test_a_missing_constraint_answer_is_an_error(self):
        rub = rubric()
        partial = answers({d['id']: 3 for d in rub['dimensions'] if 'levels' in d}, addressed={'constraint_fit.1': 0.9})
        row = {'label': 'Proposal A', 'source': 'a.md', 'answers': partial}
        self.assertIn('no answer for constraint_fit.2', c.score_row(row, rub, c.parse_constraints(BRIEF))['error'])

    def test_the_request_carries_each_constraint_in_structured_instructions(self):
        questions = c.build_request(BRIEF, 'p', rubric())['questions']
        self.assertEqual(questions['constraint_fit.1']['instructions']['constraint'], 'Nothing may depend on SSH.')
        self.assertEqual(sorted(k for k in questions if k.startswith('constraint_fit')), ['constraint_fit.1', 'constraint_fit.2'])

    def test_a_brief_without_constraints_cannot_start(self):
        err = io.StringIO()
        with mock.patch.object(c.sys, 'stderr', err), self.assertRaises(SystemExit) as caught:
            c.main(['show-request', '--brief', str(ROOT / 'README.md'), '--proposals', str(EXAMPLE / 'wsl2.md')], out=io.StringIO())
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("no 'Hard constraints:' list", err.getvalue())


class AnonymiseTests(unittest.TestCase):
    PROPOSALS = [('claude.md', 'aaa'), ('codex.md', 'bbb'), ('skyler.md', 'ccc')]

    def test_deterministic_shuffle_and_the_judge_never_sees_the_source(self):
        first, again = c.anonymise(self.PROPOSALS, seed=3), c.anonymise(self.PROPOSALS, seed=3)
        self.assertEqual(first, again)
        self.assertEqual([i['label'] for i in first], ['Proposal A', 'Proposal B', 'Proposal C'])
        self.assertEqual(sorted(i['source'] for i in first), ['claude.md', 'codex.md', 'skyler.md'])
        orders = {tuple(i['source'] for i in c.anonymise(self.PROPOSALS, seed=s)) for s in range(12)}
        self.assertGreater(len(orders), 1)                       # order really varies with the seed
        judge = ScriptedJudge({'aaa': flat(1), 'bbb': flat(2), 'ccc': flat(3)})
        c.judge_all('brief', first, rubric(), judge)
        self.assertFalse(any('.md' in text for text in judge.seen))


class DecisionTests(unittest.TestCase):
    def run_council(self, by_text, rub=None):
        rub = rub or score_rubric()
        items = c.anonymise([(f'{k}.md', k) for k in by_text], seed=0)
        return c.build_report('brief', c.judge_all('brief', items, rub, ScriptedJudge(by_text)), rub, 'scripted', 0)

    def test_weighted_total_is_normalised_per_dimension(self):
        top = {d['id']: len(d['levels']) - 1 for d in score_rubric()['dimensions']}
        report = self.run_council({'best': answers(top), 'worst': flat(0)})
        totals = {s['source']: s['total'] for s in report['scored']}
        self.assertAlmostEqual(totals['best.md'], 1.0)
        self.assertEqual(totals['worst.md'], 0.0)
        self.assertEqual(report['decision']['action'], 'recommend')
        self.assertEqual(report['decision']['recommended_source'], 'best.md')

    def test_a_veto_removes_the_top_scorer(self):
        report = self.run_council({'flashy': flat(3, vetoes={'violates_hard_constraint': 0.93}), 'sound': flat(2)})
        d = report['decision']
        self.assertEqual((d['action'], d['recommended_source']), ('recommend', 'sound.md'))
        self.assertTrue(any('vetoed by violates_hard_constraint' in r for r in d['reasons']))

    def test_close_call_escalates(self):
        a = flat(2); b = flat(2); b['evidence'] = score_answer(2.2, 3)  # 0.1 * 0.2/3 = 0.007 apart
        d = self.run_council({'a': a, 'b': b})['decision']
        self.assertEqual(d['action'], 'escalate')
        self.assertTrue(any('below min_margin' in r for r in d['reasons']))

    def test_low_judge_confidence_escalates_even_with_a_clear_lead(self):
        d = self.run_council({'lead': flat(3, confidence=0.2), 'trail': flat(1)})['decision']
        self.assertEqual(d['action'], 'escalate')
        self.assertTrue(any('below min_confidence' in r for r in d['reasons']))

    def test_judge_failure_is_recorded_and_escalates(self):
        report = self.run_council({'ok': flat(3), 'broken': c.JudgeError('TypeSafe API returned HTTP 529')})
        self.assertEqual(report['decision']['action'], 'escalate')
        self.assertIn('HTTP 529', json.dumps(report['scored']))

    def test_everything_vetoed_recommends_nothing(self):
        d = self.run_council({'x': flat(3, vetoes={'leaves_decision_unanswered': 0.9})})['decision']
        self.assertEqual((d['action'], d['recommendation']), ('escalate', None))

    def test_missing_answer_is_an_error_not_a_zero(self):
        partial = flat(3); del partial['evidence']
        scored = self.run_council({'p': partial, 'q': flat(1)})['scored']
        self.assertIn('no score for evidence', next(s for s in scored if s['source'] == 'p.md')['error'])

    def test_rescore_changes_the_winner_without_calling_a_judge(self):
        careful = flat(1); careful['failure_handling'] = score_answer(3, 3)
        simple = flat(1); simple['simplicity'] = score_answer(2, 2)
        report = self.run_council({'careful': careful, 'simple': simple})
        self.assertEqual(report['decision']['recommended_source'], 'careful.md')
        reweighted = json.loads(json.dumps(score_rubric()))
        for d in reweighted['dimensions']:
            d['weight'] = 0.9 if d['id'] == 'simplicity' else 0.02
        with mock.patch.object(c.TypeSafeJudge, 'judge', side_effect=AssertionError('no judge call allowed')):
            again = c.rescore(report, reweighted)
        self.assertEqual(again['decision']['recommended_source'], 'simple.md')
        reworded = json.loads(json.dumps(reweighted))
        reworded['dimensions'][0]['levels'][0] = 'a different level the judge never saw'
        with self.assertRaisesRegex(ValueError, 'same questions'):
            c.rescore(report, reworded)
        reweighted['dimensions'][0]['id'] = 'renamed'
        with self.assertRaises(ValueError):
            c.rescore(report, reweighted)


class TypeSafeJudgeTests(unittest.TestCase):
    OK = json.dumps({'model': 'jev-1.13.0', 'answers': {'constraint_fit': {'type': 'score', 'score': 2.4}},
                     'usage': {'input_tokens': 10, 'output_tokens': 2}}).encode()

    def judge(self, replies, calls=None, clock=None, **kw):
        clock = clock or FakeClock()
        return c.TypeSafeJudge(api_key=kw.pop('api_key', 'k'), opener=scripted_opener(replies, calls),
                               sleep=clock.sleep, clock=clock, rand=lambda: 0.0, **kw)

    def test_sends_the_documented_request(self):
        sent = {}

        def opener(request, timeout):
            sent.update(url=request.full_url, method=request.get_method(), auth=request.get_header('Authorization'),
                        body=json.loads(request.data), timeout=timeout)
            return Response(self.OK)

        result = c.TypeSafeJudge(api_key='sk-test', opener=opener).judge('b', 'p', rubric())
        self.assertEqual((sent['url'], sent['method'], sent['auth']),
                         ('https://api.typesafe.ai/v1/systemone', 'POST', 'Bearer sk-test'))
        self.assertEqual(sent['body'], c.build_request('b', 'p', rubric()))
        self.assertEqual((result['model'], result['attempts']), ('jev-1.13.0', 1))
        self.assertEqual(result['request_sha256'], c.sha256(json.dumps(c.build_request('b', 'p', rubric())).encode()))

    def test_retries_what_the_typesafe_sdk_retries_with_its_backoff(self):
        clock = FakeClock()
        replies = [http_error(429), http_error(529), http_error(503), Response(self.OK)]
        result = self.judge(replies, clock=clock, max_attempts=4).judge('b', 'p', rubric())
        self.assertEqual(clock.naps, [0.5, 1.0, 2.0])
        self.assertEqual(result['attempts'], 4)
        self.assertEqual(c.retry_delay_s(None, 9, rand=lambda: 0.0), 5.0)    # capped
        self.assertEqual(c.retry_delay_s(None, 1, rand=lambda: 1.0), 0.375)  # a quarter taken off at most

    def test_connection_failures_and_a_body_cut_short_are_retried(self):
        for failure in (urllib.error.URLError('reset'), TimeoutError(), http.client.IncompleteRead(b'{"an'),
                        http.client.RemoteDisconnected('gone')):
            result = self.judge([failure, Response(self.OK)]).judge('b', 'p', rubric())
            self.assertEqual(result['attempts'], 2, failure)

    def test_honours_a_numeric_retry_after_and_ignores_a_date(self):
        clock = FakeClock()
        replies = [http_error(429, {'Retry-After': '7'}),
                   http_error(529, {'Retry-After': 'Wed, 23 Sep 2026 10:00:00 GMT'}), Response(self.OK)]
        self.judge(replies, clock=clock).judge('b', 'p', rubric())
        self.assertEqual(clock.naps, [7.0, 1.0])

    def test_one_deadline_covers_attempts_waits_and_reads(self):
        calls = []
        with self.assertRaisesRegex(c.JudgeError, 'pass the 60 s deadline'):
            self.judge([http_error(429, {'Retry-After': '600'})], calls).judge('b', 'p', rubric())
        self.assertEqual(len(calls), 1)                    # gave up at once instead of sleeping ten minutes
        clock = FakeClock()

        def slow(request, timeout):
            calls.append(timeout)
            clock.now += timeout                           # every attempt times out after its full allowance
            raise TimeoutError()

        calls.clear()
        judge = c.TypeSafeJudge(api_key='k', opener=slow, sleep=clock.sleep, clock=clock, rand=lambda: 0.0,
                                timeout_s=25, deadline_s=60, max_attempts=9)
        with self.assertRaises(c.JudgeError):
            judge.judge('b', 'p', rubric())
        self.assertEqual(calls[:2], [25, 25])
        self.assertLess(calls[-1], 25)                     # the last attempt only gets what is left
        self.assertLessEqual(clock.now, 60)

    def test_401_stops_the_run_422_fails_with_its_detail_and_neither_leaks_the_key(self):
        calls = []
        with self.assertRaises(c.JudgeError) as caught:
            self.judge([http_error(401, body=b'bad key sk-SECRET')], calls, api_key='sk-SECRET').judge('b', 'p', rubric())
        self.assertTrue(caught.exception.fatal)
        self.assertEqual(len(calls), 1)
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('SECRET', str(caught.exception))
        body = b'{"detail": "questions.simplicity.criteria: expected an array"}'
        with self.assertRaises(c.JudgeError) as caught:
            self.judge([http_error(422, body=body)], calls, api_key='sk-SECRET').judge('b', 'p', rubric())
        self.assertFalse(caught.exception.fatal)
        self.assertEqual(len(calls), 2)
        self.assertIn('HTTP 422: {"detail": "questions.simplicity.criteria: expected an array"}', str(caught.exception))

    def test_gives_up_after_max_attempts_and_needs_at_least_one(self):
        calls = []
        with self.assertRaises(c.JudgeError):
            self.judge([http_error(429)] * 3, calls, max_attempts=3).judge('b', 'p', rubric())
        self.assertEqual(len(calls), 3)
        for bad in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                c.TypeSafeJudge(api_key='k', max_attempts=bad)

    def test_unreachable_garbage_and_missing_key(self):
        with self.assertRaisesRegex(c.JudgeError, 'unreachable'):
            self.judge([urllib.error.URLError('nope')] * 3).judge('b', 'p', rubric())
        for body in (b'<html>', b'\xff\xfe not utf-8', b'{"answers": {"x": {"noul": NaN}}}'):
            with self.assertRaisesRegex(c.JudgeError, 'not valid JSON'):
                self.judge([Response(body)]).judge('b', 'p', rubric())
        for body in (b'{"oops":1}', b'[]', b'null'):
            with self.assertRaisesRegex(c.JudgeError, 'no answers'):
                self.judge([Response(body)]).judge('b', 'p', rubric())
        with mock.patch.dict(c.os.environ, {}, clear=True), self.assertRaisesRegex(c.JudgeError, 'not set'):
            c.TypeSafeJudge()


class OllamaJudgeTests(unittest.TestCase):
    def reply(self, picked):
        return lambda request, timeout: Response(json.dumps({'message': {'content': json.dumps(picked)}}).encode())

    def test_levels_become_score_answers_with_no_confidence(self):
        picked = {d['id']: 2 for d in rubric()['dimensions']} | {v['id']: False for v in rubric()['vetoes']}
        picked['violates_hard_constraint'] = True
        out = c.OllamaJudge(opener=self.reply(picked)).judge('b', 'p', rubric())['answers']
        self.assertEqual((out['evidence']['score'], out['evidence']['confidence']), (2.0, None))
        self.assertEqual((out['violates_hard_constraint']['noul'], out['leaves_decision_unanswered']['noul']), (1.0, 0.0))

    def test_constraint_booleans_become_nouls_with_no_confidence(self):
        picked = {d['id']: 2 for d in rubric()['dimensions']} | {v['id']: False for v in rubric()['vetoes']}
        picked |= {'constraint_fit.1': True, 'constraint_fit.2': False}
        out = c.OllamaJudge(opener=self.reply(picked)).judge(BRIEF, 'p', rubric())['answers']
        self.assertEqual(out['constraint_fit.1'], {'type': 'noul', 'noul': 1.0, 'confidence': None})
        sent = {}

        def opener(request, timeout):
            sent.update(json.loads(request.data))
            return self.reply(picked)(request, timeout)

        c.OllamaJudge(opener=opener).judge(BRIEF, 'p', rubric())
        system = sent['messages'][0]['content']
        self.assertIn('false means: The proposal is silent on this constraint', system)
        self.assertIn('Nothing may depend on SSH.', system)
        row = {'label': 'Proposal A', 'source': 'a.md', 'answers': out}
        fit = c.score_row(row, rubric(), c.parse_constraints(BRIEF))['dimensions']['constraint_fit']
        self.assertEqual((fit['normalised'], fit['confidence']), (0.5, None))

    def test_out_of_range_level_is_an_error(self):
        picked = {d['id']: 99 for d in rubric()['dimensions']}
        with self.assertRaises(c.JudgeError):
            c.OllamaJudge(opener=self.reply(picked)).judge('b', 'p', rubric())


    def test_an_omitted_or_non_boolean_veto_is_an_error_not_a_pass(self):
        complete = {d['id']: 2 for d in rubric()['dimensions']} | {v['id']: False for v in rubric()['vetoes']}
        for broken in ({k: v for k, v in complete.items() if k != 'violates_hard_constraint'},
                       complete | {'violates_hard_constraint': 'false'},
                       complete | {'violates_hard_constraint': 0}):
            with self.assertRaisesRegex(c.JudgeError, 'violates_hard_constraint'):
                c.OllamaJudge(opener=self.reply(broken)).judge('b', 'p', rubric())

    def test_a_reply_that_is_not_an_object_is_an_error(self):
        for picked in ([], None, 42, 'text'):
            with self.assertRaises(c.JudgeError):
                c.OllamaJudge(opener=self.reply(picked)).judge('b', 'p', rubric())
        for raw in (b'{"message": {"content": "{\\"a\\": NaN}"}}', b'\xff', b'[]'):
            with self.assertRaises(c.JudgeError):
                c.OllamaJudge(opener=lambda r, timeout, raw=raw: Response(raw)).judge('b', 'p', rubric())

    def test_http_errors_and_a_cut_short_body_are_reported_as_such(self):
        missing = http_error(404, body=b'{"error":"model \'qwen2.5:7b\' not found"}')
        with self.assertRaisesRegex(c.JudgeError, "HTTP 404: .*not found"):
            c.OllamaJudge(opener=scripted_opener([missing])).judge('b', 'p', rubric())
        with self.assertRaisesRegex(c.JudgeError, 'unreachable: IncompleteRead'):
            c.OllamaJudge(opener=scripted_opener([http.client.IncompleteRead(b'{')])).judge('b', 'p', rubric())

    def test_only_http_urls(self):
        with self.assertRaises(ValueError):
            c.OllamaJudge('file:///etc/passwd')


class AnswerValidationTests(unittest.TestCase):
    def row(self, mutate):
        a = flat(2)
        mutate(a)
        return c.score_row({'label': 'Proposal A', 'source': 'a.md', 'answers': a}, score_rubric())

    def test_malformed_answers_are_errors_not_repairs(self):
        cases = {
            'outside 0..3': lambda a: a['evidence'].update(score=999),
            'no score': lambda a: a['evidence'].update(score=True),
            'no probability per level': lambda a: a['evidence'].pop('probabilities'),
            'no probability per level ': lambda a: a['evidence']['probabilities'].update({'2': True}),
            'do not sum to 1': lambda a: a['evidence']['probabilities'].update({'0': 0.5}),
            'does not match its probabilities': lambda a: a['evidence'].update(score=0.5),
            'a confidence outside': lambda a: a['evidence'].update(confidence=float('nan')),
            'a probability outside 0..1 for veto': lambda a: a['violates_hard_constraint'].update(noul=float('nan')),
            'a probability outside 0..1 for veto ': lambda a: a['violates_hard_constraint'].update(noul=True),
        }
        for expected, mutate in cases.items():
            self.assertIn(expected.strip(), self.row(mutate).get('error', ''), expected)

    def test_a_nan_veto_on_the_leader_can_no_longer_recommend(self):
        lead = flat(3)
        lead['violates_hard_constraint']['noul'] = float('nan')
        rub = score_rubric()
        items = c.anonymise([('lead.md', 'lead'), ('trail.md', 'trail')], seed=0)
        report = c.build_report('brief', c.judge_all('brief', items, rub, ScriptedJudge({'lead': lead, 'trail': flat(1)})),
                                rub, 'scripted', 0)
        self.assertEqual(report['decision']['action'], 'escalate')

    def test_saved_reports_with_nan_do_not_load(self):
        with self.assertRaises(ValueError):
            c.loads('{"noul": NaN}')
        with self.assertRaises(ValueError):
            c.loads('{"noul": -Infinity}')


class PolicyTests(unittest.TestCase):
    def run_council(self, by_text, rub=None):
        rub = rub or score_rubric()
        items = c.anonymise([(f'{k}.md', k) for k in by_text], seed=0)
        return c.build_report('brief', c.judge_all('brief', items, rub, ScriptedJudge(by_text)), rub, 'scripted', 0)

    def test_missing_confidence_escalates_unless_the_run_allows_it(self):
        lead = flat(3)
        lead['evidence']['confidence'] = None
        d = self.run_council({'lead': lead, 'trail': flat(1)})['decision']
        self.assertEqual(d['action'], 'escalate')
        self.assertTrue(any('no confidence for' in r and 'evidence' in r for r in d['reasons']), d['reasons'])
        rub = score_rubric()
        rub['decision']['allow_missing_confidence'] = True
        d = self.run_council({'lead': lead, 'trail': flat(1)}, rub)['decision']
        self.assertEqual(d['action'], 'recommend')
        self.assertTrue(any('allow_missing_confidence' in r for r in d['reasons']))

    def test_a_sole_survivor_must_clear_the_floor(self):
        d = self.run_council({'weak': flat(0), 'vetoed': flat(3, vetoes={'violates_hard_constraint': 0.95})})['decision']
        self.assertEqual((d['action'], d['recommended_source']), ('escalate', 'weak.md'))
        self.assertTrue(any('below the floor min_total' in r for r in d['reasons']), d['reasons'])

    def test_a_refused_key_stops_the_run(self):
        refused = c.JudgeError('TypeSafe API returned HTTP 401', fatal=True)
        judge = ScriptedJudge({'a': refused, 'b': refused, 'c': refused})
        with self.assertRaises(c.JudgeError):
            c.judge_all('brief', c.anonymise([('a.md', 'a'), ('b.md', 'b'), ('c.md', 'c')]), score_rubric(), judge)
        self.assertEqual(len(judge.seen), 1)


class ShadowGateTests(unittest.TestCase):
    LIVE = EXAMPLE / 'live-run.jev-1.13.0.json'

    def live(self, mandatory=None):
        return c.rescore(json.loads(self.LIVE.read_text(encoding='utf-8')), rubric(), mandatory)

    def run_council(self, by_text, rub=None):
        return PolicyTests.run_council(self, by_text, rub)

    def test_the_live_run_matches_the_independent_exact_enumeration(self):
        report = self.live()
        r = report['shadow']['ranking']
        self.assertAlmostEqual(r['p_flip'], 0.069923845, places=8)          # Codex's lattice, ties counted
        self.assertAlmostEqual(r['expected_loss'], 0.003102757, places=8)
        self.assertAlmostEqual(r['p_flip_together'], 0.2659, places=6)
        self.assertEqual((report['decision']['action'], report['shadow']['action']), ('escalate', 'recommend'))

    def test_a_mandatory_constraint_the_leader_may_miss_is_an_open_check(self):
        shadow = self.live(mandatory=[6])['shadow']
        self.assertEqual(shadow['action'], 'escalate')
        self.assertIn('mandatory constraint 6 is open on Proposal A: p=0.53', shadow['reasons'][0])
        with self.assertRaisesRegex(ValueError, 'constraints 1 to 7'):
            self.live(mandatory=[8])

    def test_a_runner_up_that_cannot_reach_the_leader_gives_zero_and_a_tie_counts_as_a_flip(self):
        report = self.run_council({'best': flat(3), 'worst': flat(1)})
        self.assertEqual(report['shadow']['ranking']['p_flip'], 0.0)
        self.assertEqual(report['shadow']['action'], 'recommend')
        tied = self.run_council({'a': flat(2), 'b': flat(2)})['shadow']['ranking']
        self.assertEqual(tied['p_flip'], 1.0)

    def test_a_vetoed_high_scorer_or_a_leader_near_the_threshold_is_flagged(self):
        report = self.run_council({'flashy': flat(3, vetoes={'violates_hard_constraint': 0.72}), 'sound': flat(2)})
        self.assertEqual(report['decision']['recommended_source'], 'sound.md')    # the operative gate is unchanged
        self.assertEqual(report['shadow']['action'], 'escalate')
        self.assertTrue(any('vetoed only by answers within 0.06' in r for r in report['shadow']['reasons']))
        report = self.run_council({'lead': flat(3, vetoes={'violates_hard_constraint': 0.66}), 'trail': flat(1)})
        self.assertEqual(report['decision']['action'], 'recommend')
        self.assertTrue(any('would remove the leader' in r for r in report['shadow']['reasons']))
        far = self.run_council({'flashy': flat(3, vetoes={'violates_hard_constraint': 0.9}), 'sound': flat(2)})
        self.assertEqual(far['shadow']['action'], 'recommend')

    def test_an_answer_without_a_distribution_makes_ranking_unavailable(self):
        lead, trail = flat(3, confidence=None), flat(1, confidence=None)
        report = self.run_council({'lead': lead, 'trail': trail})
        self.assertIn('unavailable', report['shadow']['ranking'])
        self.assertIn('ranking uncertainty unavailable', c.render(report))

    def test_both_distributions_keep_all_the_mass_and_the_mean(self):
        items = [(0.5, [(0.0, 0.3), (1.0, 0.7)]), (0.5, [(0.0, 0.4), (0.5, 0.2), (1.0, 0.4)])]
        independent, together = c.total_distribution(items), c.total_distribution(items, together=True)
        for dist in (independent, together):
            self.assertAlmostEqual(sum(p for _, p in dist), 1.0)
            self.assertAlmostEqual(sum(t * p for t, p in dist), 0.5 * 0.7 + 0.5 * 0.5)
        # one shared draw u: u < 0.3 both lowest; 0.3-0.4 only the first is up; 0.4-0.6 the second is at 0.5
        self.assertEqual([t for t, _ in together], [0.0, 0.5, 0.75, 1.0])
        for (_, p), expected in zip(together, (0.3, 0.1, 0.2, 0.4)):
            self.assertAlmostEqual(p, expected)
        self.assertAlmostEqual(c.expected_loss([(0.5, 1.0)], [[(0.0, 0.5), (1.0, 0.5)]]), 0.25)


class CheckpointAndJournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_each_finished_proposal_is_saved_and_a_stopped_run_cannot_be_rescored(self):
        class StopsOnSecond:
            name, info, calls = 'fake', {'url': 'x'}, 0

            def __init__(self, *a, **kw):
                pass

            def judge(self, brief, proposal, rub):
                StopsOnSecond.calls += 1
                if StopsOnSecond.calls == 2:
                    raise c.JudgeError('TypeSafe API returned HTTP 401', fatal=True)
                return {'answers': {}, 'model': 'fake', 'usage': None}

        out = self.dir / 'run.json'
        err = io.StringIO()
        with mock.patch.object(c, 'TypeSafeJudge', StopsOnSecond), mock.patch.object(c.sys, 'stderr', err):
            code = c.main(['judge', *CliTests.ARGS, '--out', str(out)], out=io.StringIO())
        self.assertEqual(code, 2)
        self.assertIn('HTTP 401', err.getvalue())
        saved = json.loads(out.read_text())
        self.assertEqual((saved['complete'], len(saved['judged'])), (False, 1))
        self.assertFalse(list(self.dir.glob('*.tmp')))
        with self.assertRaisesRegex(ValueError, 'stopped part-way'):
            c.rescore(saved, rubric())

    def test_journal_label_and_validate(self):
        report, journal = self.dir / 'r.json', self.dir / 'decisions.jsonl'
        args = ['rescore', '--report', str(EXAMPLE / 'live-run.jev-1.13.0.json'), '--rubric', str(c.DEFAULT_RUBRIC),
                '--out', str(report), '--journal', str(journal)]
        self.assertEqual(c.main(args, out=io.StringIO()), 1)
        self.assertEqual(c.main(['label', '--journal', str(journal), '--brief', str(EXAMPLE / 'brief.md'),
                                 '--pick', 'examples/coordinator-host/measure-then-decide.md'], out=io.StringIO()), 0)
        run, label = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual((run['operative']['action'], run['shadow']['action']), ('escalate', 'recommend'))
        self.assertEqual(run['report_sha256'], c.sha256(report.read_bytes()))
        self.assertEqual((label['pick'], label['brief_sha256']), ('measure-then-decide.md', run['brief_sha256']))
        out = io.StringIO()
        self.assertEqual(c.main(['validate', '--journal', str(journal)], out=out), 0)
        rows = {line.split()[0]: line.split() for line in out.getvalue().splitlines() if line.startswith(('operative', 'shadow'))}
        self.assertEqual(rows['operative'][1:5], ['1', '0', '0', '0%'])
        self.assertEqual(rows['shadow'][1:5], ['1', '1', '0', '100%'])

    def test_journal_needs_a_saved_report(self):
        with mock.patch.object(c.sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit):
            c.main(['rescore', '--report', 'x', '--rubric', 'y', '--journal', 'j'])

    def test_the_bound_reproduces_the_research_sample_sizes(self):
        for confidence, limit, needed in ((0.90, 0.20, (11, 18, 25)), (0.95, 0.10, (29, 46, 61))):
            for wrong, n in enumerate(needed):
                self.assertLessEqual(c.wrong_rate_bound(wrong, n, confidence), limit)
                self.assertGreater(c.wrong_rate_bound(wrong, n - 1, confidence), limit)
        self.assertEqual(c.wrong_rate_bound(0, 0, 0.9), 1.0)


class CliTests(unittest.TestCase):
    ARGS = ['--brief', str(EXAMPLE / 'brief.md'), '--proposals',
            *(str(EXAMPLE / n) for n in ('native-windows.md', 'wsl2.md', 'measure-then-decide.md'))]

    def test_show_request_prints_every_payload_and_touches_no_network(self):
        out = io.StringIO()
        with mock.patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('no network allowed')):
            self.assertEqual(c.main(['show-request', *self.ARGS], out=out), 0)
        text = out.getvalue()
        self.assertEqual(text.count('"model": "jev-latest"'), 3)
        self.assertIn('Remote access to the desktop is NOT configured', text)

    def test_a_glob_that_matches_the_brief_leaves_it_out(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(c.sys, 'stderr', err):
            self.assertEqual(c.main(['show-request', '--brief', str(EXAMPLE / 'brief.md'), '--proposals',
                                     *map(str, sorted(EXAMPLE.glob('*.md')))], out=out), 0)
        self.assertEqual(out.getvalue().count('"state"'), 3)
        self.assertNotIn('<- brief.md', out.getvalue())
        self.assertIn('left out of the proposals', err.getvalue())
        self.assertIn('read 7 hard constraints from', err.getvalue())

    def test_show_request_can_pin_a_model_version(self):
        out = io.StringIO()
        self.assertEqual(c.main(['show-request', '--typesafe-model', 'jev-1.13.0', *self.ARGS], out=out), 0)
        self.assertEqual(out.getvalue().count('"model": "jev-1.13.0"'), 3)

    def test_report_names_the_model_that_answered_and_the_tokens_used(self):
        judged = [{'label': 'Proposal A', 'source': 'a.md', 'answers': flat(2), 'model': 'jev-1.13.0',
                   'usage': {'input_tokens': 900, 'output_tokens': 40}},
                  {'label': 'Proposal B', 'source': 'b.md', 'error': 'TypeSafe API returned HTTP 529'}]
        head = c.render(c.build_report('brief', judged, score_rubric(), 'typesafe', 0)).splitlines()[0]
        self.assertIn('model: jev-1.13.0', head)
        self.assertIn('input tokens: 900', head)

    def test_rescore_of_the_saved_live_run_needs_no_key_and_exits_like_judge(self):
        saved = str(EXAMPLE / 'live-run.jev-1.13.0.json')
        out = io.StringIO()
        with mock.patch.dict(c.os.environ, {}, clear=True), \
                mock.patch.object(c.urllib.request, 'urlopen', side_effect=AssertionError('no network allowed')):
            code = c.main(['rescore', '--report', saved, '--rubric', str(c.DEFAULT_RUBRIC)], out=out)
        report = json.loads(Path(saved).read_text(encoding='utf-8'))
        self.assertEqual(code, 0 if report['decision']['action'] == 'recommend' else 1)
        self.assertIn('model: jev-1.13.0', out.getvalue())

    def test_input_errors_exit_2_not_1(self):
        live = str(EXAMPLE / 'live-run.jev-1.13.0.json')
        bad_json = Path(tempfile.mkdtemp()) / 'bad.json'
        bad_json.write_text('{"schema_version": 2, "x": NaN}')
        reworded = Path(tempfile.mkdtemp()) / 'reworded.json'
        r = json.loads(c.DEFAULT_RUBRIC.read_text())
        r['dimensions'][1]['levels'][0] = 'something else'
        reworded.write_text(json.dumps(r))
        for args in (['rescore', '--report', 'no-such-file.json', '--rubric', str(c.DEFAULT_RUBRIC)],
                     ['rescore', '--report', str(bad_json), '--rubric', str(c.DEFAULT_RUBRIC)],
                     ['rescore', '--report', live, '--rubric', str(reworded)],
                     ['show-request', '--brief', 'no-such-brief.md', '--proposals', 'x.md']):
            err = io.StringIO()
            with mock.patch.object(c.sys, 'stderr', err):
                self.assertEqual(c.main(args, out=io.StringIO()), 2, args)
            self.assertIn('error:', err.getvalue())

    def test_a_run_the_judge_answered_none_of_exits_2(self):
        class Down:
            name, info = 'fake', {}

            def __init__(self, *a, **kw):
                pass

            def judge(self, *a):
                raise c.JudgeError('TypeSafe API unreachable: URLError')

        with mock.patch.object(c, 'TypeSafeJudge', Down), mock.patch.object(c.sys, 'stderr', io.StringIO()):
            self.assertEqual(c.main(['judge', *self.ARGS], out=io.StringIO()), 2)

    def test_runs_under_python_dash_OO(self):
        done = subprocess.run([sys.executable, '-OO', str(ROOT / 'council.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_golden_render_of_the_saved_live_run(self):
        out = io.StringIO()
        c.main(['rescore', '--report', str(EXAMPLE / 'live-run.jev-1.13.0.json'), '--rubric', str(c.DEFAULT_RUBRIC)], out=out)
        self.assertEqual(out.getvalue(), (ROOT / 'tests' / 'golden' / 'live-run.rescore.txt').read_text(encoding='utf-8'))

    def test_judge_without_a_key_exits_2_and_says_why(self):
        err = io.StringIO()
        with mock.patch.dict(c.os.environ, {}, clear=True), mock.patch.object(c.sys, 'stderr', err):
            self.assertEqual(c.main(['judge', *self.ARGS], out=io.StringIO()), 2)
        self.assertIn('TYPESAFE_API_KEY is not set', err.getvalue())


if __name__ == '__main__':
    unittest.main()
