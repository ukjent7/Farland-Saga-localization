"""Shard scheduling for the workflow regression script.

The workflow regressions dominate the suite runtime, so ``localize.py test``
can run them as several balanced subprocesses. Every shard records its own
proof and timing files, and the parent folds them back into the shared
records once all shards finish, which keeps the audit trail identical to a
serial run.
"""
import json
import os
from pathlib import Path

from path_compat import WORK


SHARD_ENV = 'FARLAND_TEST_SHARD'
RECORDS = ('replay_checks', 'test_durations')


def current_shard():
    """Return ``(index, jobs)`` when this process is one shard, else ``None``."""
    raw = os.environ.get(SHARD_ENV)
    if not raw:
        return None
    index, jobs = (int(part) for part in raw.split('/'))
    if index < 1 or jobs < 1 or index > jobs:
        raise ValueError(f'Invalid shard selector: {raw}')
    return index, jobs


def artifact_path(kind, shard=None):
    """Proof/timing file for this process: the shared one, or its shard slice."""
    if shard is None:
        shard = current_shard()
    if shard is None:
        return WORK / 'maintenance' / f'{kind}.json'
    index, jobs = shard
    return WORK / 'maintenance' / f'{kind}.shard-{index}-of-{jobs}.json'


def recorded_durations():
    """Per-test wall-clock seconds from the most recent completed run."""
    path = WORK / 'maintenance' / 'test_durations.json'
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return {}


def balance(names, jobs):
    """Split test names into ``jobs`` groups of near-equal expected runtime.

    Longest-processing-time-first over the recorded durations keeps one slow
    test from deciding the wall time. Unknown tests count as the median known
    duration, so a newly added test cannot starve a single shard. Without any
    recording the names are dealt round-robin.
    """
    names = list(names)
    if jobs <= 1:
        return [names]
    groups = [[] for _ in range(jobs)]
    known = recorded_durations()
    if not known:
        for position, name in enumerate(sorted(names)):
            groups[position % jobs].append(name)
        return groups
    default = sorted(known.values())[len(known) // 2]
    order = sorted(names, key=lambda name: (-known.get(name, default), name))
    loads = [0.0] * jobs
    for name in order:
        target = min(range(jobs), key=lambda index: (loads[index], index))
        groups[target].append(name)
        loads[target] += known.get(name, default)
    return groups


def clear_shards(jobs):
    """Drop leftover shard records so a crashed run cannot poison the merge."""
    for kind in RECORDS:
        for index in range(1, jobs + 1):
            artifact_path(kind, (index, jobs)).unlink(missing_ok=True)


def merge_shards(jobs):
    """Fold every shard's proof and timing records into the shared files."""
    proof = WORK / 'maintenance' / 'replay_checks.json'
    combined = {}
    if proof.exists():
        for item in json.loads(proof.read_text(encoding='utf-8')):
            combined[json.dumps(item, sort_keys=True)] = item
    for index in range(1, jobs + 1):
        shard = artifact_path('replay_checks', (index, jobs))
        if not shard.exists():
            continue
        for item in json.loads(shard.read_text(encoding='utf-8')):
            combined[json.dumps(item, sort_keys=True)] = item
        shard.unlink()
    proof.write_text(json.dumps(list(combined.values()), ensure_ascii=False, indent=2))
    durations = WORK / 'maintenance' / 'test_durations.json'
    merged = json.loads(durations.read_text(encoding='utf-8')) if durations.exists() else {}
    for index in range(1, jobs + 1):
        shard = artifact_path('test_durations', (index, jobs))
        if not shard.exists():
            continue
        merged.update(json.loads(shard.read_text(encoding='utf-8')))
        shard.unlink()
    durations.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
