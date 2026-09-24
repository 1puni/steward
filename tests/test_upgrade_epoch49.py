"""An explicit stopped-copy conversion retains obligations and native identity."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3

import pytest
import yaml

from steward_harness.state import ConversationId, StateDatabase, TaskId, TaskSpec, TurnId
from steward_harness.task_store import GitTaskStore, PREFIX, ProcedureRun

spec = importlib.util.spec_from_file_location('upgrade_epoch49', Path(__file__).parents[1] / 'scripts/upgrade-epoch49.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def legacy(tmp_path, status='completed'):
    root = tmp_path / 'source'
    root.mkdir(mode=0o700)
    database = root / 'state.db'
    turn = 'turn_' + '1' * 32
    historical = 'turn_' + '2' * 32
    stamp = '2026-09-24T09:00:00+00:00'
    with sqlite3.connect(database) as connection:
        connection.executescript((Path(__file__).parent / 'fixtures/epoch49.sql').read_text())
        connection.execute('INSERT INTO steward_schema VALUES (1,49,?)',(stamp,))
        connection.execute('INSERT INTO turns (turn_id,conversation_id,source_event_key,operator_id,state,input_text,status_reason,provider,generation,provider_session_id,started_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (turn,'telegram:7','source:1','operator',status,'original request',
             'reconciled by operator' if status=='interrupted' else None,'codex',3,'native-session',stamp,stamp))
        connection.execute('INSERT INTO turns (turn_id,conversation_id,source_event_key,operator_id,state,input_text,status_reason,started_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?)',
            (historical,'rhythm:night','source:2','rhythm','interrupted','historical work','stopped',stamp,stamp))
        connection.execute('INSERT INTO world_turns (event_id,world_root,base_sha,candidate_sha,applied_base,applied_sha,output,provider,model,provider_session_id,generation,profile,status,reply_text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (turn,'/retained/world','a'*40,'b'*40,'a'*40,'c'*40,'original reply','codex','model','native-session',3,'balanced','accepted','visible reply',stamp))
    store = GitTaskStore(root / 'state.db.tasks.git')
    tree = store.git('mktree',input_text='')
    candidate = store.git('commit-tree',tree,input_text='product input\n')
    procedure = ProcedureRun(name='review',instructions='Review.',provider='codex',model={'model':'model'},access='read-only',identity='a'*64,candidate=candidate,base=candidate)
    task_id,_ = store.create(TaskSpec('app','Review','Accepted review request.'),procedure=procedure)
    store.input(task_id,'note','Keep this operator note.',source='operator:7')
    old,definition,body = store.read(task_id)
    work = store.git('commit-tree',tree,'-p',candidate,input_text='Legacy checkpoint\n\nDisposition: idle\n')
    fields = definition.model_dump(mode='json',exclude_none=True)
    fields.update(work=work,publication=None)
    fields['procedure']['result']='pass'
    blob = store.git('hash-object','-w','--stdin',input_text='---\n'+yaml.safe_dump(fields)+'---\n\n'+body+'\n')
    document = store.git('mktree',input_text=f'100644 blob {blob}\ttask.md\n')
    accepted = store.git('commit-tree',document,'-p',old,'-p',work,input_text='checkpoint idle\n')
    store.git('update-ref',PREFIX+str(task_id),accepted,old)
    lineage = {key:dict(provider='codex',profile='balanced',generation=3,provider_session_id='session-'+str(index))
               for index,key in enumerate(('telegram:7',str(ConversationId.for_task(task_id)),'rhythm:night'))}
    (root/'lineage.json').write_text(json.dumps(lineage))
    return database,task_id,accepted,work,lineage,turn,historical


@pytest.mark.parametrize('status',['completed','interrupted'])
def test_conversion_preserves_history_pending_input_verdict_and_lineage(tmp_path,status):
    source,task_id,accepted,work,lineage,turn,historical=legacy(tmp_path,status)
    original=hashlib.sha256(source.read_bytes()).hexdigest()
    destination=tmp_path/'converted'
    result=upgrade.convert(source,destination)
    assert result['complete'] and result['turns']==2 and result['lineages']==3
    assert result['preserved_verdicts']==1
    assert result['reconciled_accepted_turns']==(status=='interrupted')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==original
    assert GitTaskStore(source.parent/'state.db.tasks.git',create=False).refs()[PREFIX+str(task_id)]==accepted
    state=StateDatabase(destination/'state.db')
    task=state.tasks.get(task_id)
    # The retained note still owes a slice; conversion must not consume it.
    assert task.status.value=='queued' and task.verdict=='pass'
    assert task.outcome==accepted and task.brief=='Accepted review request.'
    assert [item[2] for item in task.pending]==['Keep this operator note.']
    assert state.tasks.contains(work,task.work_sha)
    assert state.tasks.git('rev-parse',work+'^{tree}')==state.tasks.git('rev-parse',task.work_sha+'^{tree}')
    for key,entry in lineage.items():
        retained=state.lineage(ConversationId(key))
        assert (retained.provider,retained.profile,retained.generation,retained.provider_session_id)==tuple(entry[k] for k in ('provider','profile','generation','provider_session_id'))
    assert state.get_turn(TurnId(historical)).conversation_id.workspace=='rhythm-night'
    with state.connect() as connection:
        row=connection.execute('SELECT * FROM turns WHERE turn_id=?',(turn,)).fetchone()
        assert row['state']=='completed' and row['reply_text']=='visible reply'
        assert row['candidate_sha']=='b'*40
    report=json.loads((destination/'epoch50-conversion.json').read_text())
    if status=='interrupted':
        assert report['reconciled_accepted_turns'][0]['status_reason']=='reconciled by operator'


@pytest.mark.parametrize('boundary',['running','pending-world','completion','wrong-epoch','existing-destination'])
def test_conversion_refuses_unsafe_source_before_copying(tmp_path,boundary):
    source,*_=legacy(tmp_path)
    destination=tmp_path/'converted'
    with sqlite3.connect(source) as connection:
        if boundary=='running':
            connection.execute("UPDATE turns SET state='running',completed_at=NULL,status_reason=NULL WHERE source_event_key='source:1'")
        elif boundary=='pending-world':
            connection.execute("UPDATE world_turns SET status='pending',reply_text=NULL")
        elif boundary=='wrong-epoch':
            connection.execute('UPDATE steward_schema SET epoch=48')
    if boundary=='completion':
        directory=source.with_suffix('.world-completions');directory.mkdir();(directory/'pending.json').write_text('{}')
    if boundary=='existing-destination': destination.mkdir()
    with pytest.raises(ValueError): upgrade.convert(source,destination)
    assert not (destination/'state.db').exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="requires real ownership changes")
def test_root_conversion_preserves_shared_directory_and_file_owners(tmp_path):
    source,*_=legacy(tmp_path)
    shared=source.parent/'shared-inbox'
    shared.mkdir(mode=0o750)
    note=shared/'retained.txt';note.write_text('retained input')
    os.chown(shared,65534,65534)
    os.chown(note,65534,65534)
    destination=tmp_path/'converted'
    upgrade.convert(source,destination)
    for original in (shared,note):
        copied=destination/original.relative_to(source.parent)
        assert (copied.stat().st_uid,copied.stat().st_gid,copied.stat().st_mode)==(original.stat().st_uid,original.stat().st_gid,original.stat().st_mode)
