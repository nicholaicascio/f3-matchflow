"""Collect raw counters with explicit input semantics. Never infer billing."""
import argparse
import json
from pathlib import Path

import lifecycle as life


def counters(value):
    if not isinstance(value, dict):
        raise ValueError('raw counters unavailable')
    result = {}
    for key in ('input', 'output', 'reasoning', 'cacheRead'):
        number = value.get(key)
        if type(number) is not int or number < 0:
            raise ValueError('missing/invalid raw counter: ' + key)
        result[key] = number
    return result


def normalize(raw, semantics):
    raw = counters(raw)
    if semantics == 'unknown':
        return None
    total = raw['input'] + raw['cacheRead'] if semantics == 'uncached' else raw['input']
    if semantics not in ('uncached', 'total') or raw['cacheRead'] > total:
        raise ValueError('input semantics conflict with cache counters')
    return dict(input_tokens=total, output_tokens=raw['output'], cached_tokens=raw['cacheRead'])


def headless_events(path):
    total = dict(input=0, output=0, reasoning=0, cacheRead=0)
    sessions, seen, count = set(), {}, 0
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        event = json.loads(line)
        part = event.get('part') or {}
        if part.get('type') != 'step-finish':
            continue
        sid = event.get('sessionID')
        if not sid:
            raise ValueError('usage event missing session')
        sessions.add(sid)
        tokens = part.get('tokens') or {}
        raw = counters(dict(input=tokens.get('input'), output=tokens.get('output'),
                            reasoning=tokens.get('reasoning'), cacheRead=(tokens.get('cache') or {}).get('read')))
        identity = part.get('id')
        if identity and identity in seen:
            if seen[identity] != raw:
                raise ValueError('conflicting duplicate usage event')
            continue
        if identity:
            seen[identity] = raw
        count += 1
        for key in total:
            total[key] += raw[key]
    if not count or len(sessions) != 1:
        raise ValueError('no usage or mixed-session event stream')
    return dict(session_id=next(iter(sessions)), raw=total, events=count)


def collect(folder, method, input_path=None, semantics='unknown'):
    folder = Path(folder)
    manifest = life.manifest(folder)
    snapshot = life.load(input_path) if input_path else None
    if method == 'workflow':
        snapshot = snapshot.get('data', snapshot)
        supplied = {a['sessionId']: a.get('usage') for a in snapshot['agents']}
    elif method == 'subagents':
        # Adapter contract for exported session metadata; missing counters stay missing.
        supplied = {a['session_id']: a.get('raw') for a in snapshot['sessions']} if snapshot else {}
    elif method != 'headless':
        raise ValueError('unknown launch method')
    rows = []
    for task in manifest['tasks']:
        state = life.snapshot(folder, task['id'])
        try:
            if method == 'headless':
                item = headless_events(folder / task['id'] / 'events.jsonl')
                if item['session_id'] != state['session_id']:
                    raise ValueError('event session does not match lifecycle')
                raw = item['raw']
            else:
                if method == 'subagents' and snapshot is None:
                    saved = life.load(folder/task['id']/'usage-raw.json')
                    if saved['session_id'] != state['session_id']:
                        raise ValueError('usage session does not match lifecycle')
                    tokens = saved.get('raw_session_tokens') or {}
                    raw = counters(dict(input=tokens.get('input'), output=tokens.get('output'),
                        reasoning=tokens.get('reasoning'), cacheRead=(tokens.get('cache') or {}).get('read')))
                else:
                    raw = counters(supplied[state['session_id']])
            rows.append(dict(worker=task['id'], session_id=state['session_id'], raw=raw,
                             service_usage=normalize(raw, semantics), input_semantics=semantics))
        except (KeyError, ValueError, OSError) as exc:
            rows.append(dict(worker=task['id'], raw=None, service_usage=None, error=str(exc)))
    result = dict(method=method, captured_utc=life.stamp(), source=str(input_path) if input_path else
                  'bound-session-metadata' if method == 'subagents' else 'events.jsonl',
                  workers=rows, billed_cost=None, billing_note='Not measured; zero harness cost is not a bill.')
    life.save(folder / 'usage.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder'); parser.add_argument('--method', required=True, choices=['workflow', 'subagents', 'headless'])
    parser.add_argument('--input'); parser.add_argument('--input-semantics', default='unknown', choices=['unknown', 'uncached', 'total'])
    args = parser.parse_args()
    try:
        print(json.dumps(collect(args.folder, args.method, args.input, args.input_semantics), indent=2))
    except (ValueError, KeyError, OSError, AttributeError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
