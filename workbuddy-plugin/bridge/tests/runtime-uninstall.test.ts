import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';
import {uninstallRuntime,type UninstallDependencies} from '../src/runtime-uninstall.js';
const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const input={operation_id:'uninstall-1',confirmed:true as const,keep_project_data:true};
function setup(t:{after:(fn:()=>void)=>unknown}){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-uninstall-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));const store=new RuntimeStore(registry,dir,'fixture');store.transition('awaiting_consent');store.transition('preparing');store.transition('verifying',{checks:[{code:'health_ok',passed:true}]});store.transition('ready',{version:'0.1.0'});return store;}
function deps():UninstallDependencies{return {projectDeletionConfirmed:false,stopBackend:async()=>{},removeComponents:async()=>[{kind:'components',removed:true}],deleteProjects:async()=>[{kind:'project_data',removed:true}]};}
test('component uninstall never silently deletes project data or external copies',async t=>{const store=setup(t);let deletes=0,removes=0;const d={...deps(),removeComponents:async()=>{removes++;return [{kind:'components' as const,removed:true}];},deleteProjects:async()=>{deletes++;return [];}};const a=await uninstallRuntime(registry,store,input,d);await uninstallRuntime(registry,store,input,d);assert.equal(a.state,'not_initialized');assert.deepEqual(a.retained,['external_copies','project_data']);assert.equal(deletes,0);assert.equal(removes,1);});
test('project deletion requires separate confirmation',async t=>{const store=setup(t);await assert.rejects(()=>uninstallRuntime(registry,store,{...input,keep_project_data:false},deps()),/PROJECT_DELETION_CONFIRMATION_REQUIRED/);assert.equal(store.snapshot().state,'ready');const a=await uninstallRuntime(registry,store,{...input,keep_project_data:false},{...deps(),projectDeletionConfirmed:true});assert.equal(a.state,'not_initialized');assert.deepEqual(a.retained,['external_copies']);});
test('partial removal reports retained resources rather than full deletion',async t=>{const store=setup(t);const result=await uninstallRuntime(registry,store,input,{...deps(),removeComponents:async()=>[{kind:'components',removed:false}]});assert.equal(result.state,'repair_required');assert.ok(result.retained.includes('components'));assert.equal(result.checks[0]?.passed,false);});
