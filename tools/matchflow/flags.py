"""The compiler options a worker may request from the trusted host."""
SAFE_FLAGS = {f.lower() for f in (
    '/MT', '/MTd', '/MD', '/MDd', '/O1', '/O2', '/Od', '/Ox', '/Ob0', '/Ob1', '/Ob2',
    '/Oi', '/Oi-', '/Os', '/Ot', '/Oy', '/Oy-', '/GS', '/GS-', '/EHsc', '/EHa', '/Gy',
    '/Gy-', '/GR', '/GR-', '/GL', '/GL-', '/Gd', '/Gr', '/Gz', '/fp:precise', '/fp:fast',
    '/fp:strict', '/arch:SSE', '/arch:SSE2', '/arch:IA32', '/Zc:wchar_t', '/Zc:wchar_t-',
    '/Zc:forScope', '/Zc:forScope-')}

PERMUTE_TRIALS = (10, 400)
PERMUTE_JOBS = (1, 4)
PERMUTE_SECONDS = (60, 600)


def alternate(value):
    """Validate a complete comma-separated alternate flag set."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError('alternate flags must be a non-empty comma-separated string')
    requested = [x.strip() for x in value.split(',') if x.strip()]
    if not requested or any(f.lower() not in SAFE_FLAGS for f in requested):
        raise ValueError('only documented codegen flags allowed; include/output/path options forbidden')
    return value


def _bounded(data, key, limits, default):
    value = data.get(key, default)
    if type(value) is not int or not limits[0] <= value <= limits[1]:
        raise ValueError('%s must be an integer in %d..%d' % (key, limits[0], limits[1]))
    return value


def permutation(data):
    """Bounded official-permuter options. Never accepts a path or command."""
    return dict(trials=_bounded(data, 'trials', PERMUTE_TRIALS, 120),
                jobs=_bounded(data, 'jobs', PERMUTE_JOBS, 2),
                timeout_seconds=_bounded(data, 'timeout_seconds', PERMUTE_SECONDS, 240))
