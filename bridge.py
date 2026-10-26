"""Offline JSON bridge for Ringo: prepare judge prompts or score supplied replies.

No models or commands are executed. The caller owns inference and its budget.
"""
import hashlib
import json
import sys
import threading
from collections import defaultdict, deque
from pathlib import Path

import council


def exchange(request):
    if not isinstance(request, dict) or set(request) - {
            'operation', 'brief', 'proposal', 'rubric', 'samples', 'replies', 'fingerprint', 'model'}:
        raise ValueError('unknown bridge request fields')
    op = request.get('operation')
    if op not in ('prepare', 'score'):
        raise ValueError('operation must be prepare or score')
    for key in ('brief', 'proposal', 'rubric'):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise ValueError(f'{key} must be nonempty text')
    samples = request.get('samples', 6)
    if type(samples) is not int or not 2 <= samples <= 25:
        raise ValueError('samples must be an integer from 2 to 25')
    brief, proposal = request['brief'], request['proposal']
    rubric = council.load_rubric(request['rubric'])
    constraints = council.parse_constraints(brief)
    if not constraints:
        raise ValueError('the brief needs a Hard constraints section')
    mandatory = council.check_mandatory(list(range(1, len(constraints) + 1)), constraints, rubric)
    questions = council.build_questions(rubric, constraints)
    prompts = [council.judge_prompt(brief, proposal, questions, k)[0] for k in range(samples)]
    policy = {'rubric': rubric, 'tool': council.TOOL_SHA256,
              'bridge': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'skill': council.sha256(council.SKILL.read_bytes())}
    policy_hash = council.sha256(json.dumps(policy, sort_keys=True).encode())
    identity = {'prompts': prompts, 'policy': policy_hash}
    fingerprint = council.sha256(json.dumps(identity, sort_keys=True).encode())
    if op == 'prepare':
        return {'version': 1, 'fingerprint': fingerprint, 'policy_sha256': policy_hash, 'requests': prompts}
    if request.get('fingerprint') != fingerprint:
        raise ValueError('council inputs or implementation changed after prepare')
    replies = request.get('replies')
    if not isinstance(replies, list) or len(replies) != samples or not all(isinstance(r, str) for r in replies):
        raise ValueError('score needs exactly one text reply per sample')
    model = request.get('model', 'external')
    if not isinstance(model, str) or not model:
        raise ValueError('model must be a nonempty label')
    # Use the existing judge parser and aggregation, including shuffled questions,
    # reversed levels and shrinkage. Repeated identical prompts consume a queue.
    responses, lock = defaultdict(deque), threading.Lock()
    for prompt, reply in zip(prompts, replies):
        responses[prompt].append(reply)

    def replay(_argv, prompt):
        with lock:
            return responses[prompt].popleft()

    judge = council.CommandJudge(['replay'], samples=samples, max_attempts=1, runner=replay)
    row = {'label': 'Proposal A', 'source': 'candidate',
           'proposal_sha256': council.sha256(proposal.encode()), **judge.judge(brief, proposal, rubric)}
    row['model'] = model
    report = council.build_report(brief, [row], rubric, 'ringo', 0,
                                  {**judge.info, 'model': model, 'bridge_fingerprint': fingerprint}, mandatory)
    return {'version': 1, 'fingerprint': fingerprint, 'report': report}


def main():
    try:
        result = exchange(council.loads(sys.stdin.read()))
    except (ValueError, OSError, KeyError, council.JudgeError) as exc:
        print(f'council bridge: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
