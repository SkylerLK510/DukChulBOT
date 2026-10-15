"""Design council: several proposals in, one ADVISORY recommendation out.

After Karpathy's llm-council, with one change of shape. There, LLMs answer, LLMs
rank each other in prose, and a chairman LLM writes the verdict. Here:

  1. proposals come from anywhere (you, Claude, Codex, a local model) as text files
  2. they are anonymised and shuffled, then a judge scores each one against an
     explicit rubric: typed Score questions per dimension (a per_constraint dimension
     asks one Noul per hard constraint in the brief instead), Noul questions for vetoes
  3. CODE is the chairman: weights, veto thresholds and a confidence gate decide
     between "recommend X" and "escalate to a human", and say why

The default judge is an LLM acting as Jev through its own CLI (claude, codex, or any
command that reads a prompt on stdin), asked several times per proposal so that
disagreement between runs gives the probabilities. TypeSafe's paid System One API
(https://docs.typesafe.ai/api) and a local Ollama model are the other judges. Raw answers
are kept in the report, so weights can change later without asking the judge again.

Standard library only. The TypeSafe key is read from TYPESAFE_API_KEY and is never
printed or written. `show-request` prints exactly what would leave the machine.
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import datetime
import hashlib
import http.client
import json
import math
import os
import platform
import random
import re
import shlex
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import defaultdict
from fractions import Fraction
from itertools import accumulate
from pathlib import Path

SCHEMA_VERSION = 3  # 3: provenance, checkpoints, mandatory constraints, the shadow gate; 2: parsed constraints
TYPESAFE_URL = 'https://api.typesafe.ai/v1/systemone'
DEFAULT_MODEL = 'jev-latest'
DEFAULT_RUBRIC = Path(__file__).parent / 'rubrics' / 'system_design.json'
SKILL = Path(__file__).parent / 'skills' / 'judge' / 'SKILL.md'  # the command judge's instructions
# Commands checked to give the plain model and nothing else: no user settings (the Claude CLI
# would otherwise load the user's default agent), skills, MCP servers, tools or saved session.
# CommandJudge also runs every command in an empty folder, so no project instructions or memory load.
JUDGE_PRESETS = {
    'claude': ('claude -p --setting-sources project --strict-mcp-config --disable-slash-commands --tools "" '
               '--no-session-persistence --model sonnet'),
    'codex': 'codex exec --skip-git-repo-check --ephemeral -s read-only --color never -',
}
TOOL_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# Retry what TypeSafe's own SDK retries (RetryPolicy defaults): 408, 429 and every 5xx,
# plus connection errors and timeouts. A duplicate POST costs about $0.0001.
RETRYABLE = frozenset({408, 429, *range(500, 600)})
FATAL = (401, 403)  # the key is refused: every later request would be too
MAX_BODY = 4 * 1024 * 1024  # bytes read from any response
MAX_OUTCOMES = 50_000  # distinct totals per proposal the shadow gate enumerates before it gives up
# Decision rules, their defaults and their ranges. Every value is a starting point, not a
# finding: min_total, max_flip and veto_band in particular are uncalibrated placeholders.
DECISION_RULES = (('min_confidence', 0.5), ('min_margin', 0.05), ('min_total', 0.25),
                  ('max_flip', 0.10), ('veto_band', 0.06))


class JudgeError(Exception):
    """The judge could not answer. Carries a message that is safe to store, and `fatal`
    when no later request can succeed either (a refused key), so the run stops."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


def _no_constant(name):
    raise ValueError(f'{name} is not a JSON number')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f'{text} is not a finite number')
    return value


def loads(text):
    """json.loads that refuses NaN, Infinity and literals such as 1e400 that overflow to
    infinity. Python accepts them by default, and a NaN slips past every threshold
    (NaN >= 0.7 is False, so a NaN veto never fires)."""
    return json.loads(text, parse_constant=_no_constant, parse_float=_finite_float)


def is_number(x, lo=-math.inf, hi=math.inf):
    """A finite int or float in [lo, hi]. bool is excluded: True would pass as 1."""
    return type(x) in (int, float) and math.isfinite(x) and lo <= x <= hi


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def retry_delay_s(headers, attempt, rand=random.random):
    """The server's numeric `retry-after-ms` or `Retry-After` when it sends one (the
    headers TypeSafe's SDK honours), otherwise the SDK's backoff: 0.5 s doubling to at
    most 5 s, with up to a quarter randomly taken off so retries spread out. A
    date-valued or missing header falls back. The caller's deadline caps both."""
    for name, scale in (('retry-after-ms', 0.001), ('Retry-After', 1.0)):
        try:
            seconds = float((headers or {}).get(name)) * scale
        except (TypeError, ValueError):
            continue
        if seconds >= 0:  # False for NaN
            return seconds
    return min(0.5 * 2 ** (attempt - 1), 5.0) * (1 - 0.25 * rand())


def error_detail(exc, secret=None):
    """A short one-line excerpt of an HTTP error body, read BEFORE the error is closed
    (a TypeSafe 422 names the offending field there; an Ollama 404 names the missing
    model), with the key scrubbed in case a server echoes it."""
    try:
        text = exc.read(2048).decode('utf-8', 'replace')
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        text = ''
    finally:
        exc.close()
    if secret:
        text = text.replace(secret, '***')  # before shortening, so no fragment of the key survives
    text = ' '.join(text.split())[:300]
    return f': {text}' if text else ''


# --- rubric -------------------------------------------------------------------------

def load_rubric(path):
    """A dimension is either a Score (`instructions` + `levels`) or `per_constraint`: one
    Noul per hard constraint listed in the brief, combined in code. Every value is
    checked before anything is judged or rescored: a NaN weight would otherwise give a
    NaN total that still gets recommended."""
    rubric = loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(rubric, dict):
        raise ValueError('a rubric is a JSON object')
    dims, vetoes = rubric.get('dimensions') or [], rubric.get('vetoes') or []
    if not isinstance(dims, list) or not isinstance(vetoes, list) or not all(isinstance(x, dict) for x in dims + vetoes):
        raise ValueError('dimensions and vetoes must be lists of objects')
    if not dims:
        raise ValueError('rubric needs at least one dimension')
    ids = [d.get('id') for d in dims] + [v.get('id') for v in vetoes]
    if not all(isinstance(i, str) and i and '.' not in i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('dimension and veto ids must be unique non-empty strings without dots')
    for d in dims:
        if not is_number(d.get('weight')) or d['weight'] <= 0:
            raise ValueError(f"{d['id']}: weight must be a positive finite number")
        if 'per_constraint' in d:
            if 'levels' in d:
                raise ValueError(f"{d['id']}: use either levels or per_constraint, not both")
            spec = d['per_constraint']
            question = spec.get('question') if isinstance(spec, dict) else None
            if not isinstance(question, str) or '`constraint`' not in question:
                raise ValueError(f"{d['id']}: per_constraint needs a question that refers to `constraint`")
            continue
        levels = d.get('levels')
        if not isinstance(levels, list) or not 2 <= len(levels) <= 10 or not all(isinstance(x, str) and x for x in levels):
            raise ValueError(f"{d['id']}: a Score takes 2 to 10 levels, each a non-empty string")
        if not d.get('instructions'):
            raise ValueError(f"{d['id']}: instructions are required")
    if not math.isfinite(sum(d['weight'] for d in dims)):
        raise ValueError('the weights must add up to a finite number')
    for v in vetoes:
        if not v.get('instructions') or not is_number(v.get('threshold'), 0, 1) or v['threshold'] == 0:
            raise ValueError(f"{v['id']}: veto needs instructions and a threshold in (0, 1]")
    decision = rubric.setdefault('decision', {})
    if not isinstance(decision, dict):
        raise ValueError('decision must be an object')
    for key, default in DECISION_RULES:
        decision.setdefault(key, default)
        if not is_number(decision[key], 0, 1):
            raise ValueError(f'decision.{key} must be a number from 0 to 1')
    if type(decision.setdefault('allow_missing_confidence', False)) is not bool:
        raise ValueError('decision.allow_missing_confidence must be true or false')
    return rubric


HEADING = re.compile(r'^\s{0,3}(?:#{1,6}\s*)?[*_]{0,2}\s*hard\s+constraints?\s*(?:\([^)]*\))?\s*[*_]{0,2}\s*:?'
                     r'\s*[*_]{0,2}\s*#*\s*$', re.IGNORECASE)
BULLET = re.compile(r'^(\s*)(?:[-*+]|\d+[.)])\s+(.*\S)\s*$')
THEMATIC_BREAK = re.compile(r'^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$')
FENCE = re.compile(r'^(\s*)(`{3,}(?=[^`]*$)|~{3,})')  # a backtick fence's info string has no backticks


def parse_constraints(brief):
    """The bullets under every `Hard constraints:` line or heading in the brief, in order,
    read the way Markdown reads a list. Text between the heading and the first bullet is
    skipped. A line straight under a bullet continues it, indented or not; a bullet
    indented deeper than the list folds into the one above; blank lines between bullets
    are fine. After a blank line an unindented line ends the list, and so does a heading,
    a horizontal rule or an unindented code fence. Nothing inside a code fence counts,
    so a brief can quote an example. No such section gives []."""
    constraints, inside, indent, after_blank, fence, fence_indent = [], False, None, False, None, 0
    # CommonMark's line endings only: str.splitlines also splits on U+2028, U+001E and others.
    for line in re.split(r'\r\n|\r|\n', brief):
        opener = FENCE.match(line)
        if fence:
            if opener and opener.group(2)[0] == fence[0] and len(opener.group(2)) >= len(fence) \
                    and not line[opener.end():].strip():
                fence = None
                continue
            # a fence inside a bullet ends with the bullet: a line indented less than the fence closes it
            if not line.strip() or len(line) - len(line.lstrip()) >= fence_indent or not fence_indent:
                continue
            fence = None
        if opener:
            fence, fence_indent = opener.group(2), len(opener.group(1))
            if inside and indent is not None and not fence_indent:
                inside = False
            continue
        if HEADING.match(line):
            inside, indent, after_blank = True, None, False
            continue
        if not inside:
            continue
        if not line.strip():
            after_blank = True
            continue
        bullet = None if THEMATIC_BREAK.match(line) else BULLET.match(line)
        if indent is None:
            if bullet:
                indent = len(bullet.group(1))
                constraints.append(bullet.group(2))
            elif line.lstrip().startswith('#'):
                inside = False
        elif THEMATIC_BREAK.match(line) or line.lstrip().startswith('#'):
            inside = False
        elif bullet and len(bullet.group(1)) <= indent:
            constraints.append(bullet.group(2))
        elif bullet:
            constraints[-1] += (' ' if constraints[-1].endswith(':') else '; ') + bullet.group(2)
        elif line[:1].isspace() or not after_blank:
            constraints[-1] += ' ' + line.strip()
        else:
            inside = False
        after_blank = False
    return [' '.join(c.split()) for c in constraints]


def per_constraint_ids(d, constraints):
    return [f"{d['id']}.{i}" for i in range(1, len(constraints) + 1)]


def build_questions(rubric, constraints=()):
    """Rubric -> the API's `questions` map. Ids are for code; the model never sees them.
    A per_constraint dimension becomes one Noul per constraint, with the constraint
    carried in structured instructions so the question can point at it by name."""
    questions = {}
    for d in rubric['dimensions']:
        if 'per_constraint' in d:
            spec = d['per_constraint']
            for qid, text in zip(per_constraint_ids(d, constraints), constraints):
                q = {'type': 'noul', 'instructions': {'constraint': text, 'question': spec['question']}}
                if spec.get('criteria'):
                    q['criteria'] = spec['criteria']
                questions[qid] = q
            continue
        questions[d['id']] = {'type': 'score', 'instructions': d['instructions'], 'criteria': list(d['levels'])}
    for v in rubric.get('vetoes', []):
        q = {'type': 'noul', 'instructions': v['instructions']}
        if v.get('criteria'):
            q['criteria'] = v['criteria']
        questions[v['id']] = q
    return questions


def build_request(brief, proposal, rubric, model=DEFAULT_MODEL):
    """Exactly what is sent for one proposal. Every proposal gets the same questions
    over the same brief, independently, so scores are comparable across proposals."""
    return {'state': {'decision_brief': brief, 'proposal': proposal},
            'model': model, 'questions': build_questions(rubric, parse_constraints(brief))}


# --- anonymisation ------------------------------------------------------------------

def anonymise(proposals, seed=0):
    """[(source_name, text[, path])] -> [{'label','source','text'[,'path']}], shuffled.
    The judge sees only the text; the label-to-source map stays in the report."""
    if len(proposals) > len(string.ascii_uppercase):
        raise ValueError('at most 26 proposals')
    order = list(proposals)
    random.Random(seed).shuffle(order)
    return [{'label': f'Proposal {string.ascii_uppercase[i]}', 'source': name, 'text': text,
             **({'path': path[0]} if path else {})}
            for i, (name, text, *path) in enumerate(order)]


# --- judges -------------------------------------------------------------------------

class TypeSafeJudge:
    name = 'typesafe'

    def __init__(self, api_key=None, url=TYPESAFE_URL, model=DEFAULT_MODEL, timeout_s=60.0, deadline_s=60.0,
                 max_attempts=3, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic,
                 rand=random.random):
        self.api_key = api_key or os.environ.get('TYPESAFE_API_KEY')
        if not self.api_key:
            raise JudgeError('TYPESAFE_API_KEY is not set')
        if type(max_attempts) is not int or max_attempts < 1 or not is_number(timeout_s) or timeout_s <= 0 \
                or not is_number(deadline_s) or deadline_s <= 0:
            raise ValueError('max_attempts must be at least 1, and timeout_s and deadline_s positive')
        self.url, self.model, self.timeout_s, self.deadline_s = url, model, timeout_s, deadline_s
        self.max_attempts, self.opener, self.sleep, self.clock, self.rand = max_attempts, opener, sleep, clock, rand
        self.info = {'url': url, 'requested_model': model, 'timeout_s': timeout_s, 'deadline_s': deadline_s,
                     'max_attempts': max_attempts}

    def judge(self, brief, proposal, rubric):
        """One proposal, every question in one request. Transient failures are retried
        within one deadline that covers attempts, waits and reads; urllib's own timeout
        is per blocking operation, so each one gets only the time that is left."""
        body = json.dumps(build_request(brief, proposal, rubric, self.model)).encode()
        request = urllib.request.Request(self.url, data=body, method='POST', headers={
            'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'})
        deadline, payload, attempt = self.clock() + self.deadline_s, None, 0
        for attempt in range(1, self.max_attempts + 1):
            # ceiling: a server that trickles bytes inside each timeout can still overrun the deadline
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise JudgeError(f'TypeSafe API gave no answer within {self.deadline_s:g} s')
            try:
                with self.opener(request, timeout=min(self.timeout_s, remaining)) as response:
                    raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise JudgeError('TypeSafe response is larger than 4 MiB')
                payload = loads(raw)
                break
            except urllib.error.HTTPError as exc:  # before URLError and OSError, which it subclasses
                failure = f'TypeSafe API returned HTTP {exc.code}{error_detail(exc, self.api_key)}'
                if exc.code in FATAL:
                    raise JudgeError(failure, fatal=True) from None
                if exc.code not in RETRYABLE or attempt == self.max_attempts:
                    raise JudgeError(failure) from None
                delay = retry_delay_s(exc.headers, attempt, self.rand)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                failure = f'TypeSafe API unreachable: {type(exc).__name__}'
                if attempt == self.max_attempts:
                    raise JudgeError(failure) from None
                delay = retry_delay_s(None, attempt, self.rand)
            except ValueError:  # bad JSON, bad UTF-8 or NaN; also a body cut short that had no Content-Length
                failure = 'TypeSafe API returned a body that is not valid JSON'
                if attempt == self.max_attempts:
                    raise JudgeError(failure) from None
                delay = retry_delay_s(None, attempt, self.rand)
            if self.clock() + delay >= deadline:
                raise JudgeError(f'{failure}; waiting {delay:g} s to retry would pass the {self.deadline_s:g} s deadline')
            self.sleep(delay)
        if not isinstance(payload, dict) or not isinstance(payload.get('answers'), dict):
            raise JudgeError('TypeSafe response has no answers map')
        return {'answers': payload['answers'], 'model': payload.get('model'), 'usage': payload.get('usage'),
                'request_sha256': sha256(body), 'attempts': attempt}


def describe(instructions):
    """Structured instructions as one line of prose for a judge that takes a prompt."""
    if isinstance(instructions, str):
        return instructions
    rest = '; '.join(f'`{k}` is {json.dumps(v)}' for k, v in instructions.items() if k != 'question')
    return f"{instructions.get('question', '')} ({rest})"


class OllamaJudge:
    """Offline stand-in so the pipeline runs with no key and nothing leaving the machine.
    An LLM forced to a JSON schema picks one level per Score and true or false per Noul:
    no probability distribution, so every answer carries confidence None, the confidence
    gate escalates unless the run allows missing confidence, and the shadow gate reports
    ranking uncertainty as unavailable. Use it to check plumbing and as a second
    opinion, not as a calibrated judge."""
    name = 'ollama'

    def __init__(self, url='http://localhost:11434', model='qwen2.5:7b', timeout_s=300.0,
                 opener=urllib.request.urlopen):
        if not re.match(r'https?://', url):
            raise ValueError('the Ollama URL must start with http:// or https://')
        self.url, self.model, self.timeout_s, self.opener = url.rstrip('/'), model, timeout_s, opener
        self.info = {'url': self.url, 'requested_model': model, 'timeout_s': timeout_s}

    def judge(self, brief, proposal, rubric):
        questions, props, lines = build_questions(rubric, parse_constraints(brief)), {}, []
        for qid, q in questions.items():
            if q['type'] == 'score':
                props[qid] = {'type': 'integer', 'minimum': 0, 'maximum': len(q['criteria']) - 1}
                levels = ' | '.join(f'{i}: {text}' for i, text in enumerate(q['criteria']))
                lines.append(f"{qid} (integer level): {describe(q['instructions'])} Levels -> {levels}")
            else:
                props[qid] = {'type': 'boolean'}
                rules = ''.join(f' {k} means: {v}' for k, v in (q.get('criteria') or {}).items())
                lines.append(f"{qid} (true/false): {describe(q['instructions'])}{rules}")
        system = ('You are a strict evaluator of engineering design proposals. `decision_brief` and '
                  '`proposal` are given as JSON. Answer every item independently and literally. '
                  'Reply only with JSON.\n' + '\n'.join(lines))
        body = json.dumps({
            'model': self.model, 'stream': False, 'options': {'temperature': 0},
            'format': {'type': 'object', 'properties': props, 'required': list(props)},
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': json.dumps({'decision_brief': brief, 'proposal': proposal})}],
        }).encode()
        request = urllib.request.Request(self.url + '/api/chat', data=body, method='POST',
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise JudgeError('Ollama reply is larger than 4 MiB')
            picked = loads(loads(raw)['message']['content'])
        except urllib.error.HTTPError as exc:  # a 404 means the model is not pulled
            raise JudgeError(f'Ollama returned HTTP {exc.code}{error_detail(exc)}') from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise JudgeError(f'Ollama unreachable: {type(exc).__name__}') from None
        except (KeyError, TypeError, ValueError):
            raise JudgeError('Ollama reply was not the requested JSON') from None
        if not isinstance(picked, dict):
            raise JudgeError('Ollama reply was not the requested JSON object')
        answers = {}
        for qid, q in questions.items():
            value = picked.get(qid)
            if q['type'] == 'noul':
                # Never read an omitted answer as false: for a veto, false lets the proposal through.
                if type(value) is not bool:
                    raise JudgeError(f'Ollama gave no true/false answer for {qid}')
                answers[qid] = {'type': 'noul', 'noul': float(value), 'confidence': None}
                continue
            top = len(q['criteria']) - 1
            if type(value) is not int or not 0 <= value <= top:
                raise JudgeError(f'Ollama gave no valid level for {qid}')
            answers[qid] = {'type': 'score', 'score': float(value), 'confidence': None,
                            'probabilities': {str(i): float(i == value) for i in range(top + 1)}}
        return {'answers': answers, 'model': f'ollama:{self.model}', 'usage': None,
                'request_sha256': sha256(body), 'attempts': 1}


def skill_prompt():
    """The command judge's instructions: skills/judge/SKILL.md without its front matter."""
    text = SKILL.read_text(encoding='utf-8')
    if text.startswith('---\n'):
        text = text.split('\n---\n', 1)[1]
    return text.strip()


def score_confidence(probabilities):
    """TypeSafe's published Score confidence, max(0, 1 - sum p_i |i - m| / MAD_unif): the expected
    distance from the most likely level m, relative to an even spread's distance from the middle
    level. It reproduces Jev's own confidence within 0.01 on all 15 live Score answers. With tied
    most likely levels the farther one is used, so a split never reads as more certain than it is."""
    p = {int(k): v for k, v in probabilities.items()}
    n, likeliest = len(p), max(p.values())
    spread = sum(abs(i - (n - 1) / 2) for i in p) / n
    distance = max(sum(v * abs(i - m) for i, v in p.items()) for m in p if p[m] == likeliest)
    return max(0.0, 1 - distance / spread)


def judge_prompt(brief, proposal, questions, sample):
    """One sample's prompt, and the map from each neutral key (q1, q2, ...) to its question id
    and, for a Score, letter -> level. The judge never sees the rubric's ids. Question order is
    shuffled per sample, and Score levels are lettered in forward order on even samples and
    reversed on odd ones, so order effects spread across samples instead of tilting all of
    them the same way (Xu et al., Findings of EMNLP 2026)."""
    ids = list(questions)
    random.Random(sample).shuffle(ids)
    lines, keys = [], {}
    for n, qid in enumerate(ids, 1):
        q, key = questions[qid], f'q{n}'
        if q['type'] == 'score':
            levels = list(enumerate(q['criteria']))[::-1 if sample % 2 else 1]
            lettered = list(zip(string.ascii_lowercase, levels))
            keys[key] = (qid, {letter: level for letter, (level, _) in lettered})
            shown = '; '.join(f'({letter}) {text}' for letter, (_, text) in lettered)
            lines.append(f"- {key}, pick one letter: {describe(q['instructions'])} Levels: {shown}")
        else:
            keys[key] = (qid, None)
            rules = ''.join(f" {k.capitalize()} means: {v.rstrip('.')}." for k, v in (q.get('criteria') or {}).items())
            lines.append(f"- {key}, true or false: {describe(q['instructions'])}{rules}")
    state = json.dumps({'decision_brief': brief, 'proposal': proposal}, indent=2, ensure_ascii=False)
    return f"{skill_prompt()}\n\n## Questions\n\n" + '\n'.join(lines) + f"\n\n## Input\n\n{state}\n", keys


def read_reply(text, keys):
    """{question id: level or bool} from a judge's reply. The reply may wrap its JSON object in
    prose or a code fence; the last object with the most keys is read. Anything else raises
    ValueError: a missing or non-bool veto is never read as false, which would let it pass."""
    decoder, found, missing = json.JSONDecoder(parse_constant=_no_constant, parse_float=_finite_float), None, None
    for start in (i for i, ch in enumerate(text) if ch == '{'):
        try:
            obj = decoder.raw_decode(text, start)[0]
        except ValueError:
            continue
        gone = [qid for key, (qid, _) in keys.items() if key not in obj] if isinstance(obj, dict) else None
        if gone is not None and (missing is None or len(gone) <= len(missing)):
            found, missing = obj, gone
    if found is None:
        raise ValueError('the reply has no JSON object')
    if missing:
        raise ValueError(f"no answer for {', '.join(missing)}")
    picks = {}
    for key, (qid, letters) in keys.items():
        value = found[key]
        if letters is None:
            if type(value) is not bool:
                raise ValueError(f'no true/false answer for {qid}')
            picks[qid] = value
            continue
        letter = value.strip().strip('()').lower() if isinstance(value, str) else None
        if letter not in letters:
            raise ValueError(f'no valid level letter for {qid}')
        picks[qid] = letters[letter]
    return picks


class CommandJudge:
    """An LLM acting as Jev through its own command-line tool, with no API bill. Each sample runs
    one command (round robin over several) with skills/judge/SKILL.md, the questions and the input
    on stdin, in an empty folder, and reads one pick per question from the reply. Disagreement
    between samples is the uncertainty: a level's probability is the share of samples that chose
    it, a Noul's the share that said true. For a judge, sampled agreement tracked human agreement
    better than a confidence the model states (Jung et al., ICLR 2025: AUROC 0.72 against 0.55),
    but it is not calibrated either. One sample has no distribution, so its answers carry
    confidence None, as the Ollama judge's do."""
    name = 'cmd'

    def __init__(self, commands, samples=5, timeout_s=600.0, max_attempts=2, runner=None):
        if not commands:
            raise JudgeError(f"the command judge needs --judge-cmd: a preset ({', '.join(JUDGE_PRESETS)}) "
                             'or a full command that reads a prompt on stdin')
        if type(samples) is not int or not 1 <= samples <= 25 or type(max_attempts) is not int or max_attempts < 1 \
                or not is_number(timeout_s) or timeout_s <= 0:
            raise ValueError('samples must be 1 to 25, max_attempts at least 1, and timeout_s positive')
        self.commands = [JUDGE_PRESETS.get(cmd, cmd) for cmd in commands]
        self.argvs = [shlex.split(cmd) for cmd in self.commands]
        if not all(self.argvs):
            raise ValueError('a judge command is empty')
        self.samples, self.timeout_s, self.max_attempts = samples, timeout_s, max_attempts
        self.runner = runner or self.run_command
        self.info = {'commands': self.commands, 'samples': samples, 'timeout_s': timeout_s,
                     'max_attempts': max_attempts, 'skill_sha256': sha256(SKILL.read_bytes())}

    def run_command(self, argv, prompt):
        """stdout of one run, in an empty folder so no project instructions, memory or files
        reach the judge."""
        # ceiling: stdout is read whole; a command that floods it fills memory
        with tempfile.TemporaryDirectory(prefix='council-judge-') as cwd:
            done = subprocess.run(argv, input=prompt, check=False, capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', cwd=cwd, timeout=self.timeout_s)
        if done.returncode:
            tail = ' '.join(done.stderr.split())[-300:]
            raise JudgeError(f'{Path(argv[0]).name} exited with {done.returncode}' + (f': {tail}' if tail else ''))
        return done.stdout

    def sample(self, k, prompt, keys):
        """Sample k's picks and the attempts it took. A failed or unreadable reply is asked again;
        a command that cannot start stops the run, since every later sample would fail too."""
        argv, problem = self.argvs[k % len(self.argvs)], ''
        for attempt in range(1, self.max_attempts + 1):
            try:
                return read_reply(self.runner(argv, prompt), keys), attempt
            except OSError as exc:
                raise JudgeError(f'cannot run the judge command {argv[0]}: {type(exc).__name__}', fatal=True) from None
            except subprocess.TimeoutExpired:
                problem = f'no reply within {self.timeout_s:g} s'
            except (JudgeError, ValueError) as exc:
                problem = str(exc)
        raise JudgeError(f'sample {k + 1} ({Path(argv[0]).name}): {problem}')

    def judge(self, brief, proposal, rubric):
        questions = build_questions(rubric, parse_constraints(brief))
        jobs = [(k, *judge_prompt(brief, proposal, questions, k)) for k in range(self.samples)]
        with concurrent.futures.ThreadPoolExecutor(self.samples) as pool:
            results = list(pool.map(lambda job: self.sample(*job), jobs))
        picks, k = [r[0] for r in results], self.samples
        answers = {}
        for qid, q in questions.items():
            values = [p[qid] for p in picks]
            if q['type'] == 'noul':
                answers[qid] = {'type': 'noul', 'noul': sum(values) / k, **({'confidence': None} if k == 1 else {})}
                continue
            probs = {str(i): values.count(i) / k for i in range(len(q['criteria']))}
            answers[qid] = {'type': 'score', 'score': sum(values) / k, 'probabilities': probs,
                            'confidence': score_confidence(probs) if k > 1 else None}
        return {'answers': answers, 'model': ' + '.join(sorted({Path(a[0]).name for a in self.argvs})), 'usage': None,
                'request_sha256': sha256('\0'.join(job[1] for job in jobs).encode()),
                'attempts': sum(r[1] for r in results),
                'samples': [{'command': i % len(self.argvs), 'picks': p} for i, p in enumerate(picks)]}


# --- the chairman is code -----------------------------------------------------------

def judge_all(brief, anonymised, rubric, judge, on_row=None):
    """Judge every proposal in turn. Each finished row is passed to `on_row` (the CLI
    writes a checkpoint), so a crash keeps the answers already given. A fatal judge
    error (a refused key) stops the run instead of sending every remaining proposal."""
    rows = []
    for item in anonymised:
        row = {'label': item['label'], 'source': item['source'], **({'path': item['path']} if 'path' in item else {}),
               'proposal_sha256': sha256(item['text'].encode())}
        started = time.monotonic()
        try:
            row.update(judge.judge(brief, item['text'], rubric))
        except JudgeError as exc:
            if exc.fatal:
                raise
            row['error'] = str(exc)
        row['elapsed_s'] = round(time.monotonic() - started, 3)
        rows.append(row)
        if on_row:
            on_row(rows)
    return rows


def check_score(answer, top):
    """What is wrong with a Score answer over levels 0..top, or None. It errors rather
    than repairs: a clamped 999 or a missing distribution would otherwise reach the
    totals and the shadow gate looking normal."""
    if not isinstance(answer, dict) or not is_number(answer.get('score')):
        return 'no score'
    if not 0 <= answer['score'] <= top:
        return f"score {answer['score']} outside 0..{top}"
    probs = answer.get('probabilities')
    if not isinstance(probs, dict) or set(probs) != {str(i) for i in range(top + 1)} \
            or not all(is_number(p, 0, 1) for p in probs.values()):
        return f'no probability per level 0..{top}'
    if abs(sum(probs.values()) - 1) > 0.05:  # two-decimal rounding over 10 levels stays within this
        return 'probabilities that do not sum to 1'
    if abs(sum(int(k) * p for k, p in probs.items()) - answer['score']) > 0.25:
        return 'a score that does not match its probabilities'
    if answer.get('confidence') is not None and not is_number(answer['confidence'], 0, 1):
        return 'a confidence outside 0..1'
    return None


def check_noul(answer):
    if not isinstance(answer, dict) or answer.get('noul') is None:
        return 'no answer'
    if not is_number(answer['noul'], 0, 1):
        return 'a probability outside 0..1'
    if answer.get('confidence') is not None and not is_number(answer['confidence'], 0, 1):
        return 'a confidence outside 0..1'
    return None


def no_uncertainty(answer):
    """The judge says it cannot report uncertainty (`"confidence": null`, as the Ollama
    judge does), so its probabilities are picks, not distributions."""
    return 'confidence' in answer and answer['confidence'] is None


def noul_confidence(answer):
    """|2p - 1|, TypeSafe's documented confidence for a Noul: 0 at p = 0.5, 1 at p = 0
    or 1. None when the judge cannot report uncertainty."""
    return None if no_uncertainty(answer) else abs(2 * answer['noul'] - 1)


def score_per_constraint(d, answers, constraints, problems):
    """Mean probability that the proposal addresses each constraint (an expected share,
    so an unsure 0.5 counts half), with the least certain constraint as its confidence."""
    found = []
    for qid, text in zip(per_constraint_ids(d, constraints), constraints):
        problem = check_noul(answers.get(qid))
        if problem:
            problems.append(f'{problem} for {qid}')
        else:
            found.append({'constraint': text, 'probability': answers[qid]['noul'],
                          'confidence': noul_confidence(answers[qid])})
    if not constraints:
        problems.append(f"{d['id']} needs the brief's hard constraints and there are none")
    if len(found) < len(constraints) or not constraints:
        return None
    known = [c['confidence'] for c in found if c['confidence'] is not None]
    return {'normalised': sum(c['probability'] for c in found) / len(found),
            'confidence': min(known) if known else None, 'confidence_missing': len(known) < len(found),
            'constraints': found}


def score_row(row, rubric, constraints=()):
    """One judged proposal -> normalised dimensions, weighted total, vetoes, confidence.
    Values keep full precision; only the printed report rounds."""
    if row.get('error'):
        return {'label': row['label'], 'source': row['source'], 'error': row['error']}
    answers, total_weight = row.get('answers'), sum(d['weight'] for d in rubric['dimensions'])
    if not isinstance(answers, dict):
        return {'label': row['label'], 'source': row['source'], 'error': 'no answers'}
    dims, total, problems = {}, 0.0, []
    for d in rubric['dimensions']:
        if 'per_constraint' in d:
            dim = score_per_constraint(d, answers, constraints, problems)
            if dim is None:
                continue
        else:
            top, answer = len(d['levels']) - 1, answers.get(d['id'])
            problem = check_score(answer, top)
            if problem:
                problems.append(f"{problem} for {d['id']}")
                continue
            dim = {'normalised': answer['score'] / top, 'confidence': answer.get('confidence')}
        dims[d['id']] = dim
        total += dim['normalised'] * d['weight'] / total_weight
    vetoes = []
    for v in rubric.get('vetoes', []):
        answer = answers.get(v['id'])
        problem = check_noul(answer)
        if problem:
            problems.append(f"{problem} for veto {v['id']}")
        elif answer['noul'] >= v['threshold']:
            vetoes.append({'id': v['id'], 'probability': answer['noul'], 'threshold': v['threshold']})
    if problems:
        return {'label': row['label'], 'source': row['source'], 'error': '; '.join(problems)}
    known = [(dim['confidence'], k) for k, dim in dims.items() if dim['confidence'] is not None]
    lowest = min(known, key=lambda c: c[0]) if known else (None, None)
    return {'label': row['label'], 'source': row['source'], 'total': total, 'dimensions': dims, 'vetoes': vetoes,
            'min_confidence': lowest[0], 'min_confidence_at': lowest[1],
            'no_confidence': [k for k, dim in dims.items() if dim['confidence'] is None or dim.get('confidence_missing')]}


def decide(scored, rubric):
    """Recommend only when the evidence is clear; otherwise hand it to a person and
    say exactly why. A recommendation is advice. Nothing here acts on it."""
    rule, reasons = rubric['decision'], []
    for s in scored:
        if s.get('error'):
            reasons.append(f"{s['label']} could not be judged: {s['error']}")
        for v in s.get('vetoes', []):
            reasons.append(f"{s['label']} vetoed by {v['id']} (p={v['probability']:.2f} >= {v['threshold']})")
    eligible = sorted((s for s in scored if not s.get('error') and not s['vetoes']),
                      key=lambda s: s['total'], reverse=True)
    if not eligible:
        return {'action': 'escalate', 'recommendation': None, 'reasons': reasons + ['no proposal survived judging']}
    winner, blockers = eligible[0], []
    if any(s.get('error') for s in scored):
        blockers.append('at least one proposal was not judged, so the comparison is incomplete')
    if len(eligible) > 1:
        margin = winner['total'] - eligible[1]['total']
        if margin < rule['min_margin']:
            blockers.append(f"{winner['label']} leads {eligible[1]['label']} by {margin:.4f}, "
                            f"below min_margin {rule['min_margin']}")
    else:
        margin = None
    if winner['total'] < rule['min_total']:
        blockers.append(f"{winner['label']} scores {winner['total']:.3f}, below the floor min_total {rule['min_total']}")
    if winner['no_confidence']:
        missing = f"the judge reported no confidence for {winner['label']} on {', '.join(winner['no_confidence'])}"
        if rule['allow_missing_confidence']:
            reasons.append(f'{missing}, so the confidence gate was not applied there (allow_missing_confidence)')
        else:
            blockers.append(f'{missing}, so the confidence gate cannot be applied '
                            '(pass --allow-missing-confidence for an advisory run)')
    if winner['min_confidence'] is not None and winner['min_confidence'] < rule['min_confidence']:
        at, detail = winner.get('min_confidence_at'), ''
        unclear = sorted((c for c in winner['dimensions'].get(at, {}).get('constraints', [])
                          if c['confidence'] is not None and c['confidence'] < rule['min_confidence']),
                         key=lambda c: c['confidence'])
        if unclear:
            detail = ': unclear whether it addresses ' + ', '.join(
                f"\"{c['constraint']}\" (p={c['probability']:.2f})" for c in unclear)
        blockers.append(f"judge confidence on {winner['label']} drops to {winner['min_confidence']:.2f}"
                        f"{f' on {at}' if at else ''}{detail}, below min_confidence {rule['min_confidence']}")
    return {'action': 'escalate' if blockers else 'recommend', 'recommendation': winner['label'],
            'recommended_source': winner['source'], 'margin': margin, 'reasons': reasons + blockers}


# --- the shadow gate: reported next to the verdict, never in charge of it -----------

def answer_distributions(row, rubric, constraints):
    """A proposal's total as independent pieces, [(weight share, [(value, p), ...])]: one
    per Score (its levels) and one per constraint question (0 or 1). Vetoes are held
    fixed. Shares and values are exact fractions (a weight of 0.15 is 3/20), so totals
    that are equal on paper compare equal and tie. None when the judge cannot report
    uncertainty, so its probabilities are picks rather than distributions."""
    weights = {d['id']: Fraction(repr(d['weight'])) for d in rubric['dimensions']}
    total_weight, items = sum(weights.values()), []
    for d in rubric['dimensions']:
        share = weights[d['id']] / total_weight
        if 'per_constraint' in d:
            for qid in per_constraint_ids(d, constraints):
                answer = row['answers'][qid]
                if no_uncertainty(answer):
                    return None
                items.append((share / len(constraints), [(Fraction(0), 1 - answer['noul']), (Fraction(1), answer['noul'])]))
        else:
            answer, top = row['answers'][d['id']], len(d['levels']) - 1
            if no_uncertainty(answer):
                return None
            mass = sum(answer['probabilities'].values())
            items.append((share, [(Fraction(int(k), top), p / mass) for k, p in answer['probabilities'].items()]))
    return items


def total_distribution(items, together=False):
    """The exact distribution of a proposal's total, as sorted [(total, p)]. Independent
    answers are convolved. `together` is the stress case: every answer of the proposal
    follows one shared draw, so they are all good or all bad at once. None when the
    total has more than MAX_OUTCOMES distinct values (a rubric with many fine-grained
    dimensions); the shadow gate then reports ranking uncertainty as unavailable."""
    dist = defaultdict(float)
    if not together:
        dist[Fraction(0)] = 1.0
        for weight, outcomes in items:
            step = defaultdict(float)
            for total, p in dist.items():
                for value, q in outcomes:
                    if q:
                        step[total + weight * value] += p * q
            if len(step) > MAX_OUTCOMES:
                return None
            dist = step
        return sorted(dist.items())
    ladders = []
    for weight, outcomes in items:
        outcomes = sorted(outcomes)
        ladders.append((weight, [v for v, _ in outcomes], list(accumulate(q for _, q in outcomes))))
    cuts = sorted({0.0, 1.0, *(min(max(c, 0.0), 1.0) for _, _, cdf in ladders for c in cdf)})
    for a, b in zip(cuts, cuts[1:]):
        u = (a + b) / 2  # between two cuts every answer sits on one outcome
        total = sum(w * values[min(bisect.bisect_right(cdf, u), len(values) - 1)] for w, values, cdf in ladders)
        dist[total] += b - a
    return sorted(dist.items())


def _cdfs(dists):
    return [([t for t, _ in d], list(accumulate(p for _, p in d))) for d in dists]


def _p_below(values, cum, total, or_equal=False):
    i = (bisect.bisect_right if or_equal else bisect.bisect_left)(values, total)
    return cum[i - 1] if i else 0.0


def flip_probability(leader, others):
    """P(some other proposal's total >= the leader's), proposals independent of each
    other. A tie counts as a flip."""
    cdfs, stays = _cdfs(others), 0.0
    for total, p in leader:
        stays += p * math.prod(_p_below(values, cum, total) for values, cum in cdfs)
    return min(max(1.0 - stays, 0.0), 1.0)


def expected_loss(leader, others):
    """E[max(0, best other total - leader's total)]: the score given up, on average, by
    committing to the leader now rather than after every answer is known."""
    cdfs = _cdfs(others)
    support = sorted({t for values, _ in cdfs for t in values})
    best, previous = [], 0.0  # the distribution of the best other total
    for t in support:
        below = math.prod(_p_below(values, cum, t, or_equal=True) for values, cum in cdfs)
        best.append((t, below - previous))
        previous = below
    tail_p = list(accumulate((p for _, p in reversed(best)), initial=0.0))[::-1]
    tail_tp = list(accumulate((t * p for t, p in reversed(best)), initial=0.0))[::-1]
    loss = 0.0
    for total, p in leader:
        i = bisect.bisect_right(support, total)
        loss += p * (tail_tp[i] - total * tail_p[i])
    return max(loss, 0.0)


def shadow_gate(judged, scored, rubric, constraints, mandatory=()):
    """The research's proposed gate, REPORTED ONLY: `decide` still makes the call until
    replayed decisions show this one does better. It asks three questions instead of
    one weakest-link confidence check:
      ranking        is the leader really ahead? the margin, and the exact chance the
                     order flips under the judge's own distributions (ties count)
      eligibility    could a veto answer near its threshold change the pick?
      acceptability  does the leader clear the floor and every mandatory constraint?"""
    rule, reasons = rubric['decision'], []
    ok = [s for s in scored if not s.get('error')]
    eligible = sorted((s for s in ok if not s['vetoes']), key=lambda s: s['total'], reverse=True)
    if len(ok) < len(scored):
        reasons.append('at least one proposal was not judged')
    if not eligible:
        return {'action': 'escalate', 'recommendation': None, 'ranking': None,
                'reasons': reasons + ['no proposal survived judging']}
    winner, rows = eligible[0], {r['label']: r for r in judged}
    ranking = None
    if len(eligible) > 1:
        runner_up = eligible[1]
        margin = winner['total'] - runner_up['total']
        ranking = {'runner_up': runner_up['label'], 'margin': margin, 'ties': 'count as flips', 'method': 'exact'}
        if margin < rule['min_margin']:
            reasons.append(f"{winner['label']} leads {runner_up['label']} by {margin:.4f}, below min_margin {rule['min_margin']}")
        items = {s['label']: answer_distributions(rows[s['label']], rubric, constraints) for s in eligible}
        dists = {} if any(v is None for v in items.values()) else {
            (label, together): total_distribution(it, together) for label, it in items.items() for together in (False, True)}
        if not dists or any(v is None for v in dists.values()):
            why = ('the judge reports no uncertainty, so its probabilities are picks' if not dists
                   else f'more than {MAX_OUTCOMES} possible totals to enumerate exactly')
            ranking['unavailable'] = why
            reasons.append(f'ranking uncertainty unavailable: {why}')
        else:
            for suffix, together in (('', False), ('_together', True)):
                others = [d for (label, t), d in dists.items() if t == together and label != winner['label']]
                ranking['p_flip' + suffix] = flip_probability(dists[winner['label'], together], others)
                ranking['expected_loss' + suffix] = expected_loss(dists[winner['label'], together], others)
            if ranking['p_flip'] > rule['max_flip']:
                reasons.append(f"P(ranking flips) is {ranking['p_flip']:.3f}, above max_flip {rule['max_flip']}")
    band = rule['veto_band']
    for s in ok:
        answers = rows[s['label']]['answers']
        near = [v for v in rubric.get('vetoes', []) if abs(answers[v['id']]['noul'] - v['threshold']) <= band + 1e-9]
        if s is winner and near:
            reasons.append(f"{s['label']} is within {band} of the threshold on " + ', '.join(
                f"{v['id']} (p={answers[v['id']]['noul']:.2f}, threshold {v['threshold']})" for v in near)
                + ': a slightly different answer would remove the leader')
        fired = {v['id'] for v in s['vetoes']}
        if fired and fired <= {v['id'] for v in near} and s['total'] >= winner['total']:
            reasons.append(f"{s['label']} (total {s['total']:.3f}) is vetoed only by answers within {band} of the "
                           f"threshold ({', '.join(sorted(fired))}): a slightly different answer would make it the pick")
    if winner['total'] < rule['min_total']:
        reasons.append(f"{winner['label']} scores {winner['total']:.3f}, below the floor min_total {rule['min_total']}")
    need = (1 + rule['min_confidence']) / 2  # a confident yes: |2p - 1| >= min_confidence on the yes side
    for d in (d for d in rubric['dimensions'] if 'per_constraint' in d):
        for i in mandatory:
            c = winner['dimensions'][d['id']]['constraints'][i - 1]
            if c['confidence'] is None:
                reasons.append(f"mandatory constraint {i} cannot be checked on {winner['label']}: the judge reports "
                               f"no confidence on {d['id']} for \"{c['constraint']}\"")
            elif c['probability'] < need:
                reasons.append(f"mandatory constraint {i} is open on {winner['label']}: p={c['probability']:.2f} on "
                               f"{d['id']} for \"{c['constraint']}\", below {need:.2f}")
    return {'action': 'escalate' if reasons else 'recommend', 'recommendation': winner['label'],
            'recommended_source': winner['source'], 'ranking': ranking, 'reasons': reasons}


def check_mandatory(mandatory, constraints, rubric):
    """Constraint numbers (1-based, as printed) the owner marks as mandatory adoption checks."""
    if mandatory and not any('per_constraint' in d for d in rubric['dimensions']):
        raise ValueError('--mandatory needs a per_constraint dimension in the rubric')
    bad = [i for i in mandatory if not 1 <= i <= len(constraints)]
    if bad:
        raise ValueError(f'--mandatory {bad}: the brief has constraints 1 to {len(constraints)}')
    return sorted(set(mandatory))


def build_report(brief, judged, rubric, judge_name, seed, judge_info=None, mandatory=(), complete=True):
    """The full report, or with complete=False a checkpoint: the raw answers and their
    provenance only, so saving them never waits on scoring."""
    constraints = parse_constraints(brief)
    report = {'schema_version': SCHEMA_VERSION, 'complete': complete, 'created_at': now_utc(),
              'tool_sha256': TOOL_SHA256, 'python': platform.python_version(), 'judge': judge_name,
              'judge_config': judge_info or {}, 'seed': seed, 'brief_sha256': sha256(brief.encode()),
              'constraints': constraints, 'mandatory_constraints': list(mandatory), 'rubric': rubric, 'judged': judged}
    if complete:
        scored = [score_row(row, rubric, constraints) for row in judged]
        report.update(scored=scored, decision=decide(scored, rubric),
                      shadow=shadow_gate(judged, scored, rubric, constraints, mandatory))
    return report


def rescore(report, rubric, mandatory=None):
    """New weights or thresholds over the SAME raw answers. No judge call. The new
    rubric must ask exactly the same questions, or the stored answers would not apply:
    reworded levels or instructions need a new judge run."""
    version = report.get('schema_version', 1) if isinstance(report, dict) else None
    if type(version) is not int or version > SCHEMA_VERSION or not isinstance(report.get('rubric'), dict) \
            or not isinstance(report.get('judged'), list) \
            or not all(isinstance(r, dict) and isinstance(r.get('label'), str) and isinstance(r.get('source'), str)
                       for r in report['judged']):
        raise ValueError(f'this report is not one this tool can read (schema {SCHEMA_VERSION} or older)')
    if report.get('complete') is False:
        raise ValueError('this report is a checkpoint of a judge run that stopped part-way: judge again')
    constraints = report.get('constraints', [])  # schema 1 reports predate per-constraint questions
    if build_questions(rubric, constraints) != build_questions(report['rubric'], constraints):
        raise ValueError('rescore needs the same questions (ids, instructions and levels) as the original '
                         'run; only weights, thresholds and decision rules may change')
    mandatory = check_mandatory(report.get('mandatory_constraints', []) if mandatory is None else mandatory,
                                constraints, rubric)
    scored = [score_row(row, rubric, constraints) for row in report['judged']]
    return {**report, 'rubric': rubric, 'mandatory_constraints': mandatory, 'scored': scored,
            'decision': decide(scored, rubric), 'shadow': shadow_gate(report['judged'], scored, rubric, constraints, mandatory)}


# --- decision journal: what replay and validation will need -------------------------

def append_jsonl(path, record):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, allow_nan=False) + '\n')


def journal_run(path, report, report_path):
    """One line per run with both verdicts and their reasons, so the gates can later be
    compared on the decisions you label. Proposals are identified by the sha256 of their
    text, because two files in different folders can share a name."""
    rows = {row['label']: row for row in report['judged']}

    def verdict(v):
        return {'action': v['action'], 'source': v.get('recommended_source'), 'reasons': v['reasons'],
                'sha256': (rows.get(v.get('recommendation')) or {}).get('proposal_sha256')}

    append_jsonl(path, {
        'kind': 'run', 'logged_at': now_utc(), 'tool_sha256': TOOL_SHA256, 'report': str(report_path),
        'report_sha256': sha256(Path(report_path).read_bytes()), 'brief_sha256': report['brief_sha256'],
        'rubric_sha256': sha256(json.dumps(report['rubric'], sort_keys=True).encode()),
        'models': sorted({row['model'] for row in report['judged'] if row.get('model')}),
        'mandatory_constraints': report.get('mandatory_constraints', []),
        'candidates': [row.get('proposal_sha256') for row in report['judged']],
        'operative': verdict(report['decision']),
        'shadow': {**verdict(report['shadow']), 'p_flip': (report['shadow'].get('ranking') or {}).get('p_flip')}})


def picked(verdict, label):
    """Did the gate recommend the proposal you picked? By text hash when the run has one
    (older reports do not), otherwise by file name."""
    if verdict.get('sha256'):
        return verdict['sha256'] == label.get('pick_sha256')
    return verdict.get('source') == label.get('pick')


def wrong_rate_bound(wrong, n, confidence):
    """Exact one-sided (Clopper-Pearson) upper bound on the rate of wrong
    recommendations, from `wrong` out of `n` recommended."""
    if n == 0 or wrong >= n:
        return 1.0
    lo, hi = wrong / n, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = sum(math.comb(n, i) * mid ** i * (1 - mid) ** (n - i) for i in range(wrong + 1))
        lo, hi = (mid, hi) if tail > 1 - confidence else (lo, mid)
    return hi


def validate(records):
    """Risk and coverage of each gate on labelled decisions, one table per procedure
    (tool, rubric, models and mandatory constraints), so a later rescore with a tuned
    rubric adds a row instead of replacing the run you labelled. Within a procedure the
    latest run of each brief counts, against the latest label of that brief. A
    recommendation is wrong when its proposal is not the one you picked; an escalation
    is never wrong, only uncovered."""
    runs, labels = {}, {}
    for i, r in enumerate(records, 1):
        if not isinstance(r, dict) or r.get('kind') not in ('run', 'label') or 'brief_sha256' not in r \
                or (r['kind'] == 'label' and 'pick' not in r) or (r['kind'] == 'run' and not all(
                    k in r for k in ('tool_sha256', 'rubric_sha256', 'models', 'operative', 'shadow'))):
            raise ValueError(f'journal line {i} is not a record this tool wrote')
        if r['kind'] == 'run':
            procedure = (r['tool_sha256'], r['rubric_sha256'], tuple(r['models']), tuple(r.get('mandatory_constraints', [])))
            runs[procedure, r['brief_sha256']] = (i, r)
        else:
            labels[r['brief_sha256']] = (i, r)
    groups, warnings = defaultdict(list), []
    for (procedure, brief), (i, run) in runs.items():
        if brief not in labels:
            continue
        j, label = labels[brief]
        if label.get('pick_sha256') and any(run.get('candidates', [])) and label['pick_sha256'] not in run['candidates']:
            warnings.append(f"warning: the label for {label['pick']} names a proposal a run did not judge; skipped")
            continue
        groups[procedure].append((run, label, i > j))
    decisions = {brief for (_, brief) in runs if brief in labels}
    lines = [f"{len(decisions)} labelled decisions ({len({b for _, b in runs})} run, {len(labels)} labelled), "
             f"{len(groups)} procedure{'s' if len(groups) != 1 else ''}"] + warnings
    if len(groups) > 1:
        lines.append('Each procedure is counted on its own. Only one frozen in advance and run on decisions it was '
                     'not tuned on certifies anything.')
    for (tool, rubric_hash, models, mandatory), pairs in groups.items():
        late = sum(after for _, _, after in pairs)
        lines += ['', f"procedure: tool {tool[:8]}, rubric {rubric_hash[:8]}, models {', '.join(models) or 'n/a'}, "
                      f"mandatory {list(mandatory) or 'none'}"]
        if late:
            lines.append(f'  {late} of {len(pairs)} runs were logged after their label: expected when replaying a '
                         'frozen procedure, worthless if the procedure was tuned on these labels')
        lines.append(f"{'gate':10}{'decisions':>10}{'recommended':>13}{'wrong':>7}{'coverage':>10}   wrong at most (90% / 95%)")
        for gate in ('operative', 'shadow'):
            picks = [picked(r[gate], label) for r, label, _ in pairs if r[gate]['action'] == 'recommend']
            n, k, wrong = len(pairs), len(picks), picks.count(False)
            lines.append(f'{gate:10}{n:>10}{k:>13}{wrong:>7}{k / n:>10.0%}   '
                         f'{wrong_rate_bound(wrong, k, 0.90):.0%} / {wrong_rate_bound(wrong, k, 0.95):.0%}')
    lines += ['', 'Exact one-sided binomial bounds over the recommended decisions. With no wrong ones, '
              '11 support "at most 20% wrong" at 90% and 29 support "at most 10%" at 95%.']
    return '\n'.join(lines)


# --- CLI ----------------------------------------------------------------------------

ASSUMPTIONS = ("the judge's probabilities are treated as beliefs, with unknown calibration on this task; "
               "repeat-run noise is covered only by min_margin; max_flip, veto_band and min_total are "
               "uncalibrated placeholders")


def render(report):
    dims = [d['id'] for d in report['rubric']['dimensions']]
    # An alias such as jev-latest moves between releases; log the version that answered.
    models = sorted({row['model'] for row in report['judged'] if row.get('model')})
    tokens = sum((row.get('usage') or {}).get('input_tokens') or 0 for row in report['judged'])
    head = f"judge: {report['judge']}   model: {', '.join(models) or 'n/a'}   seed: {report['seed']}"
    samples = (report.get('judge_config') or {}).get('samples')
    head += f'   samples: {samples}' if samples else ''
    lines = [head + (f'   input tokens: {tokens}' if tokens else ''), '']
    header = f"{'':12}{'total':>7} " + ' '.join(f'{d[:12]:>13}' for d in dims) + '   source'
    lines.append(header)
    for s in sorted(report['scored'], key=lambda s: s.get('total', -1), reverse=True):
        if s.get('error'):
            lines.append(f"{s['label']:12}{'error':>7}  {s['error']}")
            continue
        cells = ' '.join(f"{s['dimensions'][d]['normalised']:>13.2f}" for d in dims)
        flag = '  VETOED' if s['vetoes'] else ''
        lines.append(f"{s['label']:12}{s['total']:>7.3f} {cells}   {s['source']}{flag}")
    judged = [s for s in sorted(report['scored'], key=lambda s: s.get('total', -1), reverse=True) if not s.get('error')]
    mandatory = set(report.get('mandatory_constraints', []))
    for dim in report['rubric']['dimensions']:
        if 'per_constraint' not in dim or not judged or dim['id'] not in judged[0]['dimensions']:
            continue
        lines += ['', f"{dim['id']}: probability that each proposal addresses the constraint"
                  + ('   (* mandatory)' if mandatory else '')]
        lines.append(f"{'':52}" + ''.join(f"{s['label'].split()[-1]:>6}" for s in judged))
        for i, text in enumerate(report.get('constraints', [])):
            short = text if len(text) <= 46 else text[:45] + '…'
            cells = ''.join(f"{s['dimensions'][dim['id']]['constraints'][i]['probability']:>6.2f}" for s in judged)
            lines.append(f"{'*' if i + 1 in mandatory else ' '} {i + 1}. {short:<47}{cells}")
    d = report['decision']
    verdict = (f"RECOMMEND {d['recommendation']} ({d['recommended_source']})" if d['action'] == 'recommend'
               else 'ESCALATE TO A HUMAN' + (f" (leader: {d['recommendation']})" if d['recommendation'] else ''))
    lines += ['', verdict] + [f'  - {r}' for r in d['reasons']]
    shadow = report.get('shadow')
    if shadow:
        would = (f"RECOMMEND {shadow['recommendation']}" if shadow['action'] == 'recommend'
                 else 'ESCALATE' + (f" (leader: {shadow['recommendation']})" if shadow['recommendation'] else ''))
        lines += ['', f'shadow gate (reported only; the verdict above decides): would {would}']
        r = shadow.get('ranking') or {}
        if 'p_flip' in r:
            lines.append(f"  ranking: leads {r['runner_up']} by {r['margin']:.3f}; P(flip) {r['p_flip']:.3f} with "
                         f"answers independent, {r['p_flip_together']:.3f} if each proposal's answers move together "
                         f"(ties count as flips); expected loss {r['expected_loss']:.3f} / {r['expected_loss_together']:.3f}")
        lines += [f'  - {reason}' for reason in shadow['reasons']]
        lines.append(f'  assumptions: {ASSUMPTIONS}')
    return '\n'.join(lines)


def read_proposals(paths):
    return [(Path(p).name, Path(p).read_text(encoding='utf-8').strip(), str(p)) for p in paths]


def write_json(path, data):
    """Through a temp file of its own and os.replace, so a crash never leaves half a
    report and two runs writing the same path never share a temp file."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, indent=2, allow_nan=False))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_args(argv):
    parser = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('judge', 'show-request', 'rescore'):
        p = sub.add_parser(name)
        if name == 'rescore':
            p.add_argument('--report', required=True)
            p.add_argument('--rubric', required=True)
        else:
            p.add_argument('--brief', required=True)
            p.add_argument('--proposals', nargs='+', required=True)
            p.add_argument('--rubric', default=str(DEFAULT_RUBRIC))
            p.add_argument('--seed', type=int, default=0)
            p.add_argument('--typesafe-model', default=DEFAULT_MODEL,
                           help='alias or version; pin a version such as jev-1.13.0 once thresholds are tuned')
            p.add_argument('--judge', choices=('cmd', 'typesafe', 'ollama'), default='cmd',
                           help='cmd (default): an LLM through its CLI, no API bill; typesafe: the paid Jev API')
        if name == 'show-request':
            continue
        if name == 'judge':
            p.add_argument('--judge-cmd', action='append', metavar='CMD',
                           help=f"a preset ({', '.join(JUDGE_PRESETS)}) or a command that reads the prompt on stdin; "
                                'repeat it for a panel, sampled in turn')
            p.add_argument('--samples', type=int, default=5, help='runs per proposal for the cmd judge (default 5)')
            p.add_argument('--ollama-url', default='http://localhost:11434')
            p.add_argument('--ollama-model', default='qwen2.5:7b')
        p.add_argument('--out', help='write the full JSON report here (judge: also after each proposal)')
        p.add_argument('--mandatory', nargs='+', type=int, metavar='N', default=None,
                       help='hard constraints (numbered as printed) that are mandatory adoption checks; '
                            'used by the shadow gate')
        p.add_argument('--allow-missing-confidence', action='store_true',
                       help='advisory run: recommend even when the judge reports no confidence (Ollama)')
        p.add_argument('--journal', help='append this run to a decision journal (needs --out)')
    p = sub.add_parser('label', help='record the proposal you would pick given what was known, for validate')
    p.add_argument('--journal', required=True)
    p.add_argument('--brief', required=True)
    p.add_argument('--pick', required=True, help='the proposal file you would pick, or "none"')
    p.add_argument('--note', default='')
    p = sub.add_parser('validate', help='risk and coverage of the operative and shadow gates on labelled decisions')
    p.add_argument('--journal', required=True)
    args = parser.parse_args(argv)
    if getattr(args, 'journal', None) and args.command in ('judge', 'rescore') and not args.out:
        parser.error('--journal needs --out, so the journal can point at a saved report')
    return parser, args


def main(argv=None, out=None):
    """Exit 0: recommend. 1: escalate. 2: could not run (bad input, no judge, every
    proposal failed, or a refused key)."""
    out = out or sys.stdout
    parser, args = parse_args(argv)
    try:
        return run(parser, args, out)
    except (OSError, ValueError, JudgeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


def run(parser, args, out):
    if args.command == 'validate':
        lines = Path(args.journal).read_text(encoding='utf-8').splitlines()
        print(validate([loads(line) for line in lines if line.strip()]), file=out)
        return 0
    if args.command == 'label':
        brief = Path(args.brief).read_text(encoding='utf-8').strip()
        none = args.pick == 'none'
        text = None if none else Path(args.pick).read_text(encoding='utf-8').strip()
        append_jsonl(args.journal, {'kind': 'label', 'logged_at': now_utc(), 'brief_sha256': sha256(brief.encode()),
                                    'pick': 'none' if none else Path(args.pick).name,
                                    'pick_sha256': None if none else sha256(text.encode()), 'note': args.note})
        return 0
    rubric = load_rubric(args.rubric)
    if getattr(args, 'allow_missing_confidence', False):
        rubric['decision']['allow_missing_confidence'] = True
    if args.command == 'rescore':
        report = rescore(loads(Path(args.report).read_text(encoding='utf-8')), rubric, args.mandatory)
        return finish(report, args, out)

    brief = Path(args.brief).read_text(encoding='utf-8').strip()
    constraints = parse_constraints(brief)
    per_constraint = [d['id'] for d in rubric['dimensions'] if 'per_constraint' in d]
    if per_constraint and not constraints:
        parser.error(f"the rubric asks one question per hard constraint ({', '.join(per_constraint)}), but "
                     f"{args.brief} has no 'Hard constraints:' list of bullets")
    if per_constraint:
        print(f'note: read {len(constraints)} hard constraints from {args.brief}', file=sys.stderr)
    # A glob such as examples/x/*.md also matches the brief; it is never a proposal.
    proposals = [p for p in args.proposals if Path(p).resolve() != Path(args.brief).resolve()]
    if len(proposals) < len(args.proposals):
        print(f'note: {args.brief} is the brief, so it was left out of the proposals', file=sys.stderr)
    if not proposals:
        parser.error('no proposals left once the brief is excluded')
    anonymised = anonymise(read_proposals(proposals), args.seed)
    if len({item['source'] for item in anonymised}) < len(anonymised):
        print('note: two proposals share a file name; the report keeps each one\'s path', file=sys.stderr)
    if args.command == 'show-request':
        for item in anonymised:
            print(f"# {item['label']}  <- {item['source']}", file=out)
            if args.judge == 'cmd':  # the first sample's prompt; later samples reorder questions and levels
                print(judge_prompt(brief, item['text'], build_questions(rubric, constraints), 0)[0], file=out)
            else:
                print(json.dumps(build_request(brief, item['text'], rubric, args.typesafe_model), indent=2), file=out)
        return 0
    mandatory = check_mandatory(args.mandatory or [], constraints, rubric)
    try:
        judge = (TypeSafeJudge(model=args.typesafe_model) if args.judge == 'typesafe'
                 else OllamaJudge(args.ollama_url, args.ollama_model) if args.judge == 'ollama'
                 else CommandJudge(args.judge_cmd, args.samples))
    except JudgeError as exc:
        print(f'cannot start judge: {exc}', file=sys.stderr)
        return 2

    def report_of(rows, complete=True):
        return build_report(brief, rows, rubric, judge.name, args.seed, judge.info, mandatory, complete)

    checkpoint = (lambda rows: write_json(args.out, report_of(rows, complete=False))) if args.out else None
    if checkpoint:
        checkpoint([])  # replaces any older report at --out, so a run that stops at once never leaves it looking current
    return finish(report_of(judge_all(brief, anonymised, rubric, judge, checkpoint)), args, out)


def finish(report, args, out):
    print(render(report), file=out)  # first, so a failing --out or journal path never hides a paid verdict
    if args.out:
        write_json(args.out, report)
    if args.journal:
        journal_run(args.journal, report, args.out)
    if all(s.get('error') for s in report['scored']):
        print('error: no proposal could be judged', file=sys.stderr)
        return 2
    return 0 if report['decision']['action'] == 'recommend' else 1


if __name__ == '__main__':
    raise SystemExit(main())
