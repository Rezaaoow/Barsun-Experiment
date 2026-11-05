"""Auditable, atomic participant plans; no per-trial network allocation.

Full 24-permutation cycles are partitioned into relabelled Williams quartets.
Shuffling quartets/rows balances position and first-order carryover every four
allocation tickets, while every possible order appears once per 24 tickets.
"""
import hashlib
import itertools
import json
import random
import secrets
from functools import lru_cache

VERSION = 'full24-williams-quartets-v1'
DD_VERSION = 'balanced-five-positions-v1'
BLOCKS = ('proposer_agent','responder_agent','proposer_coworker','responder_coworker')
ORDERS = tuple(itertools.permutations(BLOCKS))
_BASE = ((0,1,3,2),(1,2,0,3),(2,3,1,0),(3,0,2,1))
QUARTETS = tuple(sorted({tuple(sorted(tuple(labels[i] for i in row) for row in _BASE))
                         for labels in ORDERS}))


def initialize(connection, postgres):
    # Additive tables only; links, keys, assignments and historical outcomes are untouched.
    connection.execute('CREATE TABLE IF NOT EXISTS experiment_randomization_streams ('
        'stream_key TEXT PRIMARY KEY,study_code TEXT NOT NULL REFERENCES studies(study_code) ON DELETE CASCADE,'
        'stratum TEXT NOT NULL,module_occurrence INTEGER NOT NULL,next_ticket BIGINT NOT NULL,seed TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS experiment_randomization_allocations ('
        'participation_id TEXT NOT NULL REFERENCES study_participations(participation_id) ON DELETE CASCADE,'
        'kind TEXT NOT NULL,module_occurrence INTEGER NOT NULL,algorithm TEXT NOT NULL,'
        'stream_key TEXT REFERENCES experiment_randomization_streams(stream_key),ticket BIGINT,'
        'plan_json TEXT NOT NULL,created_at TEXT NOT NULL,'
        'PRIMARY KEY(participation_id,kind,module_occurrence),UNIQUE(stream_key,ticket))')


@lru_cache(maxsize=2048)
def _deck(seed, cycle):
    rng=random.Random(int.from_bytes(hashlib.sha256((seed+':'+str(cycle)).encode()).digest(),'big'))
    quartets=list(QUARTETS);rng.shuffle(quartets);deck=[]
    for quartet in quartets:
        rows=list(quartet);rng.shuffle(rows);deck.extend(rows)
    return tuple(deck)


def allocate_ultimatum(connection, postgres, assignment, occurrence, stratum, timestamp, legacy=None):
    """Caller holds the participant assignment lock; cursor and plan commit atomically."""
    marker='%s' if postgres else '?'
    key=(assignment['participation_id'],'ultimatum',occurrence)
    prior=connection.execute('SELECT plan_json FROM experiment_randomization_allocations '
        'WHERE participation_id='+marker+' AND kind='+marker+' AND module_occurrence='+marker,key).fetchone()
    if prior:return json.loads(prior['plan_json'])
    if legacy and len(legacy)==4 and set(legacy)==set(BLOCKS):
        plan={'algorithm':'legacy-order-preserved-v1','block_order':list(legacy),'ticket':None,'stratum':stratum}
        stream=None;ticket=None
    else:
        stream=hashlib.sha256(json.dumps([assignment['study_code'],stratum,occurrence,VERSION],separators=(',',':')).encode()).hexdigest()
        row=connection.execute('INSERT INTO experiment_randomization_streams '
            '(stream_key,study_code,stratum,module_occurrence,next_ticket,seed) VALUES('
            +','.join([marker]*6)+') ON CONFLICT(stream_key) DO UPDATE SET '
            'next_ticket=experiment_randomization_streams.next_ticket+1 RETURNING next_ticket,seed',
            (stream,assignment['study_code'],stratum,occurrence,1,secrets.token_hex(16))).fetchone()
        ticket=int(row['next_ticket'])-1;cycle,slot=divmod(ticket,24)
        order=_deck(row['seed'],cycle)[slot]
        plan={'algorithm':VERSION,'block_order':list(order),'ticket':ticket,'cycle':cycle,'slot':slot,
              'permutation_index':ORDERS.index(order),'stratum':stratum}
    connection.execute('INSERT INTO experiment_randomization_allocations '
        '(participation_id,kind,module_occurrence,algorithm,stream_key,ticket,plan_json,created_at) VALUES('
        +','.join([marker]*8)+')',(*key,plan['algorithm'],stream,ticket,json.dumps(plan,separators=(',',':')),timestamp))
    return plan


def allocate_dd(connection, postgres, assignment, occurrence, timestamp):
    marker='%s' if postgres else '?';key=(assignment['participation_id'],'dd_standalone',occurrence)
    row=connection.execute('SELECT plan_json FROM experiment_randomization_allocations '
        'WHERE participation_id='+marker+' AND kind='+marker+' AND module_occurrence='+marker,key).fetchone()
    if row:return json.loads(row['plan_json'])
    positions=[True,True,False,False,bool(secrets.randbelow(2))];random.SystemRandom().shuffle(positions)
    plan={'algorithm':DD_VERSION,'now_left':positions}
    connection.execute('INSERT INTO experiment_randomization_allocations '
        '(participation_id,kind,module_occurrence,algorithm,stream_key,ticket,plan_json,created_at) VALUES('
        +','.join([marker]*8)+')',(*key,DD_VERSION,None,None,json.dumps(plan,separators=(',',':')),timestamp))
    return plan
