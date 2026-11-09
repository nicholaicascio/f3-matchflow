"""Bounded literal lookup in official symbol metadata, never game bytes."""
import json


def lookup(path, text, limit=25):
    if not isinstance(text, str) or not 2 <= len(text) <= 160:
        raise ValueError('symbol query must be 2..160 characters of literal text')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('symbol limit must be 1..100')
    with open(path, encoding='utf-8-sig') as source:
        rows = json.load(source)['functions']
    found = []
    for row in rows:
        name, address = row.get('symbol'), row.get('address')
        if isinstance(name, str) and isinstance(address, str) and (text in name or text == address):
            if len(found) == limit:
                return dict(items=found, truncated=True)
            found.append(dict(symbol=name[:1000], address=address))
    return dict(items=found, truncated=False)
