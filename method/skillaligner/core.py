"""One joint adaptation call, with fixed retrieved candidates and strict output."""
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

def write_json(path, value):
    """Persist method artifacts without depending on executor adapters."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

FIELDS = ('Primary', 'Checks', 'Avoid', 'Fallback')


def validate_guide(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Guide requires exactly Primary, Checks, Avoid, Fallback')
    if any(not isinstance(value[k], list) or any(not isinstance(s, str) or not s.strip()
           for s in value[k]) for k in FIELDS):
        raise ValueError('Each guide field must be a list of nonempty strings')
    return value


def validate_skills(skills):
    if not isinstance(skills, list):
        raise ValueError('Skills must be a list of records')
    ids = set()
    for s in skills:
        if not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k].strip()
                                         for k in ('id', 'name', 'text')):
            raise ValueError('Each skill requires nonempty id, name, text strings')
        if s['id'] in ids or not isinstance(s.get('description', ''), str):
            raise ValueError('Duplicate skill id or invalid description')
        ids.add(s['id'])
    return skills


def retrieve(query, library, k=5):
    """Deterministic BM25, k1=1.5, b=.75; ties preserve library order.

    Tokenization/parameters are reproduction choices, not specified by the paper.
    """
    validate_skills(library)
    if type(k) is not int or k < 1:
        raise ValueError('k must be positive')
    tokens = lambda t: re.findall(r'\w+', t.lower())
    docs = [Counter(tokens(' '.join(s.get(x, '') for x in ('name', 'description', 'text')))) for s in library]
    if not docs:
        return []
    lengths = [sum(d.values()) for d in docs]
    avg = sum(lengths) / len(docs)
    scores = [0.0] * len(docs)
    for term in set(tokens(query)):
        df = sum(term in d for d in docs)
        idf = math.log(1 + (len(docs) - df + .5) / (df + .5))
        for i, d in enumerate(docs):
            tf = d[term]
            scores[i] += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * lengths[i] / avg))
    return [dict(library[i], retrieval_score=scores[i]) for i in
            sorted(range(len(docs)), key=lambda i: (-scores[i], i))[:k]]


def messages_for(query, specification, skills):
    if not isinstance(query, str) or not query.strip() or not isinstance(specification, str) or not specification.strip():
        raise ValueError('Explicit nonempty task query and execution specification are required')
    validate_skills(skills)
    template = Path(__file__).with_name('prompt.txt').read_text(encoding='utf-8')
    system, user = template.removeprefix('[SYSTEM]').split('[USER]', 1)
    # Substitute once; user content containing placeholders must remain literal.
    replacements = {'<TASK_QUERY>': query,
        '<AVAILABLE TOOLS, TOOL SCHEMAS, VALID ACTION OR OUTPUT FORMATS, RESOURCES, PERMISSIONS, INTERACTION RULES, AND HARD CONSTRAINTS>': specification,
        '<FOR EACH CANDIDATE: SKILL IDENTIFIER, NAME, DESCRIPTION, AND RETRIEVED SKILL TEXT>': json.dumps(skills, ensure_ascii=False)}
    user = re.sub('|'.join(re.escape(k) for k in replacements), lambda m: replacements[m[0]], user)
    return [{'role': 'system', 'content': system.strip()}, {'role': 'user', 'content': user.strip()}]


def adapt(query, specification, skills, output, client, *, model, max_tokens=4096):
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError('max_tokens must be positive')
    messages = messages_for(query, specification, skills)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'request.json', {'model': model, 'messages': messages,
               'temperature': 1, 'top_p': 1, 'max_tokens': max_tokens})
    write_json(output / 'inputs.json', {'query': query, 'execution_specification': specification,
               'skills': skills, 'input_sha256': hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()})
    try:
        response = client.chat.completions.create(**dict(model=model,
            messages=messages, temperature=1, top_p=1, max_tokens=max_tokens,
            response_format={'type': 'json_object'}))
        write_json(output / 'response.json', response.model_dump(mode='json'))
        choice = response.choices[0]
        if choice.finish_reason != 'stop':
            raise ValueError('Adaptation did not finish normally: ' + str(choice.finish_reason))
        guide = validate_guide(json.loads(choice.message.content))
        write_json(output / 'guide.json', guide)
        write_json(output / 'result.json', {'status': 'completed', 'model': model,
                   'usage': response.usage.model_dump() if response.usage else None})
        return guide
    except Exception as exc:
        write_json(output / 'result.json', {'status': 'failed', 'error_type': type(exc).__name__})
        raise


def render_guide(guide):
    validate_guide(guide)
    return '\n\n'.join('## ' + k + '\n' + '\n'.join('- ' + s for s in guide[k]) for k in FIELDS) + '\n'
