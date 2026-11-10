"""Read service ownership metadata without guessing absent counts or changing receipts."""


def co_workers(info):
    """Accept flat and region-level counts, requiring agreement when both exist."""
    counts = {}
    if 'co_workers' in info:
        counts['co_workers'] = info['co_workers']
    if 'regions' in info:
        regions = info['regions']
        if not isinstance(regions, dict):
            raise ValueError('function_info regions must be an object')
        if 'co_workers' in regions:
            counts['regions.co_workers'] = regions['co_workers']
    if not counts:
        raise ValueError('function_info missing co_workers (or regions.co_workers)')
    for field, count in counts.items():
        if type(count) is not int or count < 0:
            raise ValueError('function_info %s must be a nonnegative integer' % field)
    if len(set(counts.values())) != 1:
        raise ValueError('function_info conflicting co_workers and regions.co_workers')
    return next(iter(counts.values()))
