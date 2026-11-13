import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {Phase1Backend,createFixtureParams,envelope,REAL_VIDEO_TIMEOUT_MS,LONG_RUNNING_METHODS} from '../src/phase1-backend.js';
test('evidence rendering uses long-running allowance while controls remain short',()=>{assert.equal(LONG_RUNNING_METHODS.has('evidence.render'),true);assert.equal(LONG_RUNNING_METHODS.has('runtime.status'),false);assert.equal(LONG_RUNNING_METHODS.has('analysis.status'),false);});

const repo=path.resolve('../..');
test('Phase 3 allows long pose extraction without the old 180-second timeout',()=>{
  assert.ok(REAL_VIDEO_TIMEOUT_MS>=15*60*1000);assert.ok(REAL_VIDEO_TIMEOUT_MS<=60*60*1000);
});
test('Phase 1 refuses an unset data root with actual exit 2 and no stdout',async()=>{
  const backend=new Phase1Backend(repo);try{
    const state=await backend.start(null);assert.equal(state.running,false);assert.equal(state.exit_code,2);assert.equal(state.stdout_lines,0);
    assert.ok(state.diagnostics.some(l=>l.message.includes('TENNIS_DATA_ROOT is not set')));
  }finally{await backend.stop();}
});

test('real Phase 1 protocol: health, draft, replay, domain errors and malformed-line recovery',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-phase1-'));const backend=new Phase1Backend(repo);
  const call=async(method:string,params:Record<string,unknown>,simulation=true,operationId:string|null=null)=>(await backend.request(JSON.stringify(envelope(method,params,simulation,operationId)))).response;
  try{
    const started=await backend.start(root);assert.equal(started.running,true);assert.equal(started.health?.simulation,false);assert.equal((started.health?.checks as unknown[]).length,13);
    const operation='op-phase1-test';const params=createFixtureParams(operation);
    const first=await call('analysis.create',params,true,operation);assert.equal(first.ok,true);assert.equal(first.result?.state,'draft');assert.equal(first.result?.stage,'input');assert.deepEqual(first.result?.checkpoints,[]);
    const repeat=await call('analysis.create',params,true,operation);assert.equal(repeat.result?.task_id,first.result?.task_id);
    const status=await call('analysis.status',{task_id:first.result?.task_id,version:1});assert.equal(status.result?.state,'draft');
    const unknown=await call('analysis.create',createFixtureParams('op-missing','no-such-fixture'),true,'op-missing');assert.equal(unknown.error?.code,'DOMAIN_FIXTURE_UNKNOWN');
    assert.equal((await call('analysis.status',{task_id:'task-missing',version:1})).error?.code,'DOMAIN_UNKNOWN_TASK');
    const runtime={operation_id:'op-prepare',consent_confirmed:true,target_version:'0.1.0',intent:'prepare',scope_digest:'phase1'};
    assert.equal((await call('runtime.prepare',runtime,true,'op-prepare')).error?.code,'RUNTIME_BRIDGE_OWNED');
    const pid=backend.snapshot().pid;
    const parallel=await Promise.all(Array.from({length:8},()=>call('runtime.status',{runtime_requirement:'not_required'},false)));
    assert.equal(new Set(parallel.map(r=>r.request_id)).size,8);assert.ok(parallel.every(r=>r.ok));
    const malformed=await backend.request('{broken JSON');assert.equal(malformed.response.error?.code,'TRANSPORT_INVALID_JSON');
    const healthy=await call('runtime.status',{runtime_requirement:'not_required'},false);assert.equal(healthy.ok,true);assert.equal(backend.snapshot().pid,pid);
    const reused=JSON.stringify(envelope('runtime.status',{runtime_requirement:'not_required'},false));await backend.request(reused);
    assert.equal((await backend.request(reused)).response.error?.code,'REQUEST_ID_REUSED');
    assert.ok(backend.snapshot().diagnostics.some(l=>/rule=invalid_json ref=transport-/.test(l.message)));
    assert.ok(backend.snapshot().diagnostics.every(l=>!l.message.includes(root)));
    assert.deepEqual(fs.readdirSync(root).sort(),['internal','public']);
    await assert.rejects(()=>backend.request('{}\n{}'),/ONE_LINE_REQUIRED/);
    await assert.rejects(()=>backend.start(root),/BACKEND_ALREADY_RUNNING/);
    await backend.stop();assert.equal(backend.snapshot().running,false);
    assert.ok(fs.existsSync(path.join(root,'internal','metadata.sqlite')));
  }finally{await backend.stop();fs.rmSync(root,{recursive:true,force:true});}
});
