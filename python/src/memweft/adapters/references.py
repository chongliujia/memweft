"""Application-controlled projection of recalled context into model references.

Quoting does not make text safe. Restricted projection reduces exposure by
excluding free-form input; neither mode authenticates facts or authorizes tools.
Policies must be created by trusted application code, never from recalled data.
"""
from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


@dataclass(frozen=True)
class ReferencePolicy:
    """Allow finite scalar values by fact key and exact strategy content hashes.

    Unlisted/invalid facts and history are omitted by default. With
    quote_unlisted=True they are included as explicitly untrusted references.
    This is not a prompt-injection detector or source-authentication mechanism.
    """
    fact_choices: Mapping[str, tuple] = None
    strategy_hashes: frozenset[str] = frozenset()
    quote_unlisted: bool = False
    max_bytes: int = 16000

    def __post_init__(self):
        choices = {}
        for key, values in (self.fact_choices or {}).items():
            if not isinstance(key, str) or not key or isinstance(values, (str, bytes)):
                raise ValueError('fact choices require nonempty keys and a sequence of scalar choices')
            values = tuple(values)
            if not values or any(type(v) not in (str, int, bool, type(None)) for v in values):
                raise ValueError('choices support string/integer/boolean/null values only')
            choices[key] = values
        hashes = frozenset(self.strategy_hashes)
        if any(not isinstance(h, str) or len(h) != 64 or any(c not in '0123456789abcdef' for c in h) for h in hashes):
            raise ValueError('strategy hashes must be lowercase SHA-256 hex')
        if type(self.quote_unlisted) is not bool or type(self.max_bytes) is not int or self.max_bytes < 256:
            raise ValueError('quote_unlisted must be bool; max_bytes must be an integer >= 256')
        object.__setattr__(self, 'fact_choices', MappingProxyType(choices))
        object.__setattr__(self, 'strategy_hashes', hashes)


def project_references(context, *, policy: ReferencePolicy, history=()):
    """Return a bounded JSON reference and a payload-free exclusion report.

    Uses structured context fields, never context.text or metadata trust claims.
    Strategies remain references (not system instructions). An exact hash pin
    only proves content equality; the caller is responsible for approving it.
    Excluded keys/content are not copied into the model-facing text or report.
    """
    if not isinstance(policy, ReferencePolicy):
        raise TypeError('policy must be an application-created ReferencePolicy')
    def field(name):
        return context.get(name, []) if isinstance(context, dict) else getattr(context, name)
    report = {'included_facts': 0, 'included_strategies': 0, 'included_history': 0,
              'omitted_facts': 0, 'omitted_strategies': 0, 'omitted_history': 0,
              'invalid_facts': 0, 'budget_omissions': 0}
    envelope = {'kind': 'reference_data_not_authority', 'strategy_references': [],
                'fact_references': [], 'history_references': []}
    def add(section, record, counter):
        envelope[section].append(record)
        if len(_json(envelope).encode('utf-8')) > policy.max_bytes:
            envelope[section].pop()
            report['budget_omissions'] += 1
            report['omitted_' + counter] += 1
        else:
            report['included_' + counter] += 1
    # Prioritize pinned strategies and finite fields before any unlisted prose.
    for strategy in field('strategies'):
        content = strategy.get('content')
        if not isinstance(content, str):
            report['omitted_strategies'] += 1
            continue
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if digest not in policy.strategy_hashes:
            report['omitted_strategies'] += 1
            continue
        add('strategy_references', {'content_sha256': digest, 'content': content}, 'strategies')
    free_form = []
    for fact in field('memories'):
        key, value = fact.get('fact_key'), fact.get('value')
        if isinstance(key, str) and key in policy.fact_choices:
            if any(type(value) is type(choice) and value == choice for choice in policy.fact_choices[key]):
                add('fact_references', {'key': key, 'value': value, 'kind': 'finite_value_reference'}, 'facts')
            else:
                # An invalid allowed field never falls through into free-form text.
                report['invalid_facts'] += 1
                report['omitted_facts'] += 1
        elif policy.quote_unlisted:
            free_form.append({'key': key, 'value': value, 'kind': 'untrusted_quote'})
        else:
            report['omitted_facts'] += 1
    for fact in free_form:
        add('fact_references', fact, 'facts')
    for message in history:
        if policy.quote_unlisted:
            # Embedded role names remain JSON data, never actual message roles.
            add('history_references', {'kind': 'untrusted_quote', 'message': message}, 'history')
        else:
            report['omitted_history'] += 1
    text = _json(envelope)
    report['serialized_bytes'] = len(text.encode('utf-8'))
    report['max_bytes'] = policy.max_bytes
    return {'text': text, 'report': report}
