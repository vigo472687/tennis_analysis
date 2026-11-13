import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';
import {startRuntime,OriginalRequestQueue,type StartInput} from '../src/runtime-start.js';
const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const input:StartInput={operation_id:'start-1',runtime_requirement:'required'};
function setup(t:{after:(fn:()=>void)=>unknown},installed=true){const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-start-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const store=new RuntimeStore(registry,root,'fixture');if(installed){store.transition('awaiting_consent');store.transition('preparing');store.transition('verifying');store.transition('ready',{version:'0.1.0',checks:[{code:'installed',passed:true}]});store.transition('installed_stopped');}return store;}

test('not_required and unprepared requests never start a backend',async t=>{
  const store=setup(t,false);let starts=0;const deps={isRunning:()=>false,startBackend:async()=>{starts++;},verifyBackend:async()=>[{code:'healthy',passed:true}]};
  assert.throws(()=>startRuntime(registry,store,{...input,runtime_requirement:'not_required'} as unknown as StartInput,deps),/CONTRACT_INVALID/);
  await assert.rejects(()=>startRuntime(registry,store,input,deps),/RUNTIME_NOT_PREPARED/);assert.equal(starts,0);
});
test('concurrent starts use one process and verify before readiness',async t=>{
  const store=setup(t);let starts=0;const deps={isRunning:()=>starts>0,startBackend:async()=>{starts++;await new Promise(r=>setTimeout(r,10));},verifyBackend:async()=>[{code:'healthy',passed:true}]};
  const [a,b]=await Promise.all([startRuntime(registry,store,input,deps),startRuntime(registry,store,input,deps)]);assert.equal(starts,1);assert.equal(a.state,'ready');assert.deepEqual(a,b);
});
test('startup failure retains installed version and requires repair',async t=>{
  const store=setup(t);await assert.rejects(()=>startRuntime(registry,store,input,{isRunning:()=>false,startBackend:async()=>{throw new Error('private path');},verifyBackend:async()=>[]}),/BACKEND_START_FAILED/);assert.equal(store.snapshot().state,'repair_required');assert.equal(store.snapshot().version,'0.1.0');
});
test('retained original executes once, conflicts reject and cancellation prevents replay',async()=>{
  const queue=new OriginalRequestQueue();let runs=0;queue.retain('op-1','same',async()=>++runs);const [a,b]=await Promise.all([queue.resume('op-1'),queue.resume('op-1')]);assert.equal(a,b);assert.equal(runs,1);
  assert.throws(()=>queue.retain('op-1','different',async()=>null),/OPERATION_CONFLICT/);
  queue.retain('op-2','cancel',async()=>++runs);queue.cancel('op-2');await assert.rejects(()=>queue.resume('op-2'),/OPERATION_CANCELLED/);assert.equal(runs,1);
});
