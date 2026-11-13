import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';
import {repairRuntime} from '../src/runtime-repair.js';
import {OriginalRequestQueue} from '../src/runtime-start.js';
const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const input={operation_id:'repair-1',consent_confirmed:true as const,scope_digest:'scope-1'};
function setup(t:{after:(fn:()=>void)=>unknown}){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-repair-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));const store=new RuntimeStore(registry,dir,'fixture');store.transition('awaiting_consent');store.transition('preparing');store.transition('repair_required');return store;}
test('repair cannot expand the authorized scope',async t=>{const store=setup(t);let calls=0;await assert.rejects(()=>repairRuntime(registry,store,input,{authorizedScope:'different',repairArtifact:async()=>{calls++;return {version:'0.1.0',checks:[]};}}),/SCOPE_CONFIRMATION_REQUIRED/);assert.equal(calls,0);assert.equal(store.snapshot().state,'repair_required');});
test('repair verifies once and resumes a retained request only once',async t=>{const store=setup(t);let repairs=0,runs=0;const queue=new OriginalRequestQueue();queue.retain('create-1','fingerprint',async()=>++runs);const deps={authorizedScope:'scope-1',repairArtifact:async()=>{repairs++;await new Promise(r=>setTimeout(r,5));return {version:'0.1.0',checks:[{code:'health_ok',passed:true}]};}};await Promise.all([repairRuntime(registry,store,input,deps),repairRuntime(registry,store,input,deps)]);await Promise.all([queue.resume('create-1'),queue.resume('create-1')]);assert.equal(repairs,1);assert.equal(runs,1);assert.equal(store.snapshot().state,'ready');});
test('failed repair retains the original; choosing question fallback never executes it',async t=>{const store=setup(t);const queue=new OriginalRequestQueue();let runs=0;queue.retain('create-1','fingerprint',async()=>++runs);await assert.rejects(()=>repairRuntime(registry,store,input,{authorizedScope:'scope-1',repairArtifact:async()=>{throw new Error('private path');}}),/REPAIR_FAILED/);assert.ok(store.snapshot().retained.includes('original_request'));queue.cancel('create-1');await assert.rejects(()=>queue.resume('create-1'),/OPERATION_CANCELLED/);assert.equal(runs,0);});
