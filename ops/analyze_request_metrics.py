"""Summarize private timing logs without request bodies or identities."""
from pathlib import Path
import collections
import json
import sys

label, start, end = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
assert label in {"100-fix", "600-fix", "600-local", "600-selfhttps", "600-dual"}
root = Path('/home/myexperi/lifetrade-staging')
log = root / 'native-asgi-trial-20261102/uvicorn-supervised.log'
starts, ends = {}, {}
for path in [log.with_name(log.name + '.2'), log.with_name(log.name + '.1'), log]:
    if not path.exists():
        continue
    for line in path.open(errors='replace'):
        if not line.startswith('{"event":'):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if start <= record.get('at', 0) <= end:
            if record['event'] == 'request_start':
                starts[record['rid']] = record
            elif record['event'] == 'request_end':
                ends[record['rid']] = record


def stats(values):
    values = sorted(values)
    if not values:
        return {}
    def percentile(p):
        index = (len(values) - 1) * p
        lower = int(index)
        return round(values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower), 3)
    return {'count': len(values), 'min': round(values[0], 3), 'median': percentile(0.5),
            'p95': percentile(0.95), 'p99': percentile(0.99), 'max': round(values[-1], 3)}


rows = list(ends.values())
ingress = []
for request_id, row in starts.items():
    # Only the k6-generated timestamp prefix is meaningful; server UUIDs and
    # unrelated requests are excluded. Absolute values include clock skew.
    client_time = int(request_id[:12], 16) / 1000
    delta = row['at'] - client_time
    if abs(delta) < 3600:
        ingress.append(delta * 1000)

routes = collections.Counter(row['route'] for row in rows)
result = {
    'cohort': label, 'window_epoch': [start, end], 'request_starts': len(starts),
    'request_ends': len(ends), 'starts_without_end_in_window': len(starts.keys() - ends.keys()),
    'status_counts': dict(collections.Counter(row['status'] for row in rows)),
    'errors': dict(collections.Counter(row['error_type'] for row in rows if row['error_type'])),
    'max_inflight_per_worker': max((row['inflight'] for row in starts.values()), default=0),
    'max_python_threads_per_worker': max((row.get('python_threads', 0) for row in rows), default=0),
    'ingress_delta_ms_including_clock_skew': stats(ingress),
    'timings': {key: stats([row[key] for row in rows if row.get(key) is not None])
                for key in ['app_ms', 'first_db_ms', 'pool_wait_ms', 'sql_ms', 'db_scope_ms']},
    'routes': {route: {'count': count, 'app_ms': stats([row['app_ms'] for row in rows if row['route'] == route])}
               for route, count in routes.items()},
}
(root / 'test-runs/capacity-fix-20261103' / (label + '-server-metrics.json')).write_text(json.dumps(result, indent=2))
print(json.dumps({key: value for key, value in result.items() if key != 'routes'}))
