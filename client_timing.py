"""Bounded, idempotent visible-time checkpoints; legacy timing remains intact."""
from pydantic import BaseModel, Field, model_validator
from fastapi import HTTPException


class TimingCheckpoint(BaseModel):
    run_id: str = Field(min_length=1, max_length=100)
    clock_id: str = Field(min_length=1, max_length=100)
    step_id: str = Field(min_length=1, max_length=80)
    stage_code: str = Field(pattern=r'^(dd_lifetrade|ultimatum|intermission)$')
    sequence: int = Field(ge=1, le=1000000)
    visible_ms: int = Field(ge=0, le=604800000)
    uncertain_ms: int = Field(ge=0, le=604800000)
    closed: bool = False

    @model_validator(mode='after')
    def validate_uncertainty(self):
        if self.uncertain_ms > self.visible_ms:
            raise ValueError('Uncertain time cannot exceed observed visible time')
        return self


def initialize(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS experiment_client_clocks ('
        'run_id TEXT PRIMARY KEY REFERENCES instrument_runs(run_id), '
        'assignment_id TEXT NOT NULL, '
        'clock_id TEXT NOT NULL, step_instance_id TEXT NOT NULL, stage_code TEXT NOT NULL, '
        'sequence INTEGER NOT NULL, visible_ms BIGINT NOT NULL, uncertain_ms BIGINT NOT NULL, '
        'closed INTEGER NOT NULL, received_at TEXT NOT NULL)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_client_clocks_received ON experiment_client_clocks(received_at)')
    connection.execute('CREATE INDEX IF NOT EXISTS idx_client_clocks_assignment '
                       'ON experiment_client_clocks(assignment_id,step_instance_id)')
    connection.execute('CREATE TABLE IF NOT EXISTS experiment_client_clock_checkpoints ('
        'run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), sequence INTEGER NOT NULL, '
        'visible_ms BIGINT NOT NULL, uncertain_ms BIGINT NOT NULL, closed INTEGER NOT NULL, '
        'received_at TEXT NOT NULL, PRIMARY KEY(run_id,sequence))')


def save(connection, assignment, rows, postgres, decode, timestamp):
    """Caller owns transaction. Lock run before first insert as well as replays."""
    marker = '%s' if postgres else '?'
    acknowledged = []
    for row in sorted(rows, key=lambda r: (r.run_id, r.sequence)):
        run = connection.execute('SELECT participation_id,metadata FROM instrument_runs WHERE run_id='
            + marker + (' FOR UPDATE' if postgres else ''), (row.run_id,)).fetchone()
        if not run or run['participation_id'] != assignment['participation_id']:
            raise HTTPException(403, 'Time checkpoint does not belong to this invitation')
        metadata = decode(run['metadata'])
        expected_step = metadata.get('workflow_step_id') or ('default-' + row.stage_code.replace('_', '-'))
        if metadata.get('stage_code') != row.stage_code or expected_step != row.step_id:
            raise HTTPException(422, 'Time checkpoint module does not match its run')
        previous = connection.execute('SELECT * FROM experiment_client_clocks WHERE run_id=' + marker,
                                      (row.run_id,)).fetchone()
        if previous and (previous['clock_id'] != row.clock_id or previous['assignment_id'] != assignment['assignment_id']):
            raise HTTPException(409, 'One stable clock is required per attempt')
        values = (row.visible_ms, row.uncertain_ms, int(row.closed))
        saved = connection.execute('SELECT visible_ms,uncertain_ms,closed FROM experiment_client_clock_checkpoints '
            'WHERE run_id=' + marker + ' AND sequence=' + marker, (row.run_id, row.sequence)).fetchone()
        if saved:
            if tuple(saved[key] for key in ('visible_ms','uncertain_ms','closed')) != values:
                raise HTTPException(409, 'Checkpoint replay payload changed')
        else:
            if previous:
                # A delayed earlier snapshot can be acknowledged without changing totals.
                newer = row.sequence > previous['sequence']
                if (newer and (row.visible_ms < previous['visible_ms'] or row.uncertain_ms < previous['uncertain_ms']
                              or previous['closed'])) or (not newer and (row.visible_ms > previous['visible_ms']
                              or row.uncertain_ms > previous['uncertain_ms'] or row.closed)):
                    raise HTTPException(409, 'Checkpoint counter is not monotonic')
            connection.execute('INSERT INTO experiment_client_clock_checkpoints '
                '(run_id,sequence,visible_ms,uncertain_ms,closed,received_at) VALUES(' + ','.join([marker]*6) + ')',
                (row.run_id,row.sequence,*values,timestamp))
        if not previous:
            connection.execute('INSERT INTO experiment_client_clocks '
                '(run_id,assignment_id,clock_id,step_instance_id,stage_code,sequence,visible_ms,uncertain_ms,closed,received_at) '
                'VALUES(' + ','.join([marker]*10) + ')', (row.run_id,assignment['assignment_id'],row.clock_id,
                row.step_id,row.stage_code,row.sequence,*values,timestamp))
        elif row.sequence > previous['sequence']:
            connection.execute('UPDATE experiment_client_clocks SET sequence=' + marker + ',visible_ms=' + marker
                + ',uncertain_ms=' + marker + ',closed=' + marker + ',received_at=' + marker + ' WHERE run_id=' + marker,
                (row.sequence,*values,timestamp,row.run_id))
        acknowledged.append({'run_id':row.run_id,'sequence':row.sequence})
    return acknowledged
