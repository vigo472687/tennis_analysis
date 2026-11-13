import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';
import {prepareRuntime,type PrepareInput} from '../src/runtime-prepare.js';
const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const input:PrepareInput={operation_id:'prepare-1',consent_confirmed:true,target_version:'0.1.0',intent:'prepare',scope_digest:'scope-1'};
function setup(t:{after:(fn:()=>void)=>unknown}){const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-prepare-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return new RuntimeStore(registry,root,'fixture');}

test('refused confirmation, insufficient space and early cancel cause no preparation',async t=>{
  const store=setup(t);let called=0;const deps={checkSpace:()=>true,verifyArtifact:async()=>{called++;return [{code:'artifact_ok',passed:true}];}};
  assert.throws(()=>prepareRuntime(registry,store,{...input,consent_confirmed:false} as unknown as PrepareInput,deps),/CONTRACT_INVALID/);
  await assert.rejects(()=>prepareRuntime(registry,store,input,{...deps,checkSpace:()=>false}),/STORAGE_UNAVAILABLE/);
  const c=new AbortController();c.abort();await assert.rejects(()=>prepareRuntime(registry,store,input,deps,c.signal),/OPERATION_CANCELLED/);
  assert.equal(called,0);assert.equal(fs.existsSync(store.file),false);
});
test('concurrent identical preparations verify once and return authoritative readiness',async t=>{
  const store=setup(t);let called=0;const deps={checkSpace:()=>true,verifyArtifact:async()=>{called++;await new Promise(r=>setTimeout(r,10));return [{code:'artifact_ok',passed:true}];}};
  const [a,b]=await Promise.all([prepareRuntime(registry,store,input,deps),prepareRuntime(registry,store,input,deps)]);
  assert.equal(a.state,'ready');assert.deepEqual(a,b);assert.equal(called,1);
  await assert.rejects(()=>prepareRuntime(registry,store,{...input,scope_digest:'changed'},deps),/OPERATION_CONFLICT/);
});
test('failed artifact verification enters repair and does not pretend clean removal',async t=>{
  const store=setup(t);await assert.rejects(()=>prepareRuntime(registry,store,input,{checkSpace:()=>true,verifyArtifact:async()=>[{code:'hash_mismatch',passed:false}]}),/ARTIFACT_VERIFICATION_FAILED/);
  assert.equal(store.snapshot().state,'repair_required');assert.ok(store.snapshot().retained.includes('partial_components'));
});
test('cancellation during preparation keeps a recoverable partial state',async t=>{
  const store=setup(t);const c=new AbortController();await assert.rejects(()=>prepareRuntime(registry,store,input,{checkSpace:()=>true,verifyArtifact:async()=>{c.abort();return [{code:'artifact_ok',passed:true}];}},c.signal),/OPERATION_CANCELLED/);
  assert.equal(store.snapshot().state,'repair_required');
});
