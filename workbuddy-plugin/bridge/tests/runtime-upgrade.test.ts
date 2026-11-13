import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';
import {changeRuntimeVersion} from '../src/runtime-upgrade.js';
const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const input={operation_id:'upgrade-1',consent_confirmed:true as const,scope_digest:'scope-1',intent:'upgrade' as const,target_version:'0.2.0'};
function setup(t:{after:(fn:()=>void)=>unknown}){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-upgrade-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));const store=new RuntimeStore(registry,dir,'fixture');store.transition('awaiting_consent');store.transition('preparing');store.transition('verifying',{checks:[{code:'health_ok',passed:true}]});store.transition('ready',{version:'0.1.0'});fs.writeFileSync(path.join(dir,'original.mp4'),'keep');return {dir,store};}
test('verified version changes are idempotent and preserve project data',async t=>{const {dir,store}=setup(t);let calls=0;const deps={authorizedScope:'scope-1',verifyTarget:async()=>{calls++;return [{code:'target_ok',passed:true}];},restorePrevious:async()=>true};await Promise.all([changeRuntimeVersion(registry,store,input,deps),changeRuntimeVersion(registry,store,input,deps)]);assert.equal(calls,1);assert.equal(store.snapshot().version,'0.2.0');assert.equal(fs.readFileSync(path.join(dir,'original.mp4'),'utf8'),'keep');await assert.rejects(()=>changeRuntimeVersion(registry,store,{...input,target_version:'0.3.0'},deps),/OPERATION_CONFLICT/);});
test('failed upgrade restores verified previous version; failed rollback requires repair',async t=>{for(const restored of [true,false]){const {store}=setup(t);await assert.rejects(()=>changeRuntimeVersion(registry,store,input,{authorizedScope:'scope-1',verifyTarget:async()=>[{code:'hash_bad',passed:false}],restorePrevious:async()=>restored}),/ARTIFACT_VERIFICATION_FAILED/);assert.equal(store.snapshot().version,'0.1.0');assert.equal(store.snapshot().state,restored?'ready':'repair_required');assert.ok(store.snapshot().retained.includes('project_data'));}});
test('rollback uses the same verification and authorization boundaries',async t=>{const {store}=setup(t);const deps={authorizedScope:'scope-1',verifyTarget:async()=>[{code:'rollback_ok',passed:true}],restorePrevious:async()=>true};await assert.rejects(()=>changeRuntimeVersion(registry,store,{...input,scope_digest:'expanded'},deps),/SCOPE_CONFIRMATION_REQUIRED/);await changeRuntimeVersion(registry,store,{...input,intent:'rollback',target_version:'0.0.9'},deps);assert.equal(store.snapshot().version,'0.0.9');});
