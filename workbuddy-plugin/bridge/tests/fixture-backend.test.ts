import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry,type RequestEnvelope,type ResponseEnvelope} from '../src/contracts.js';
import {FixtureBackend} from '../src/fixture-backend.js';

const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
function fixture(t:{after:(fn:()=>void)=>unknown},delayMs=5,fault?:'timeout'|'crash'|'invalid_response'|'delete_partial'){
  const dataRoot=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-fixture-'));const assets=path.join(dataRoot,'assets');fs.mkdirSync(assets);
  fs.writeFileSync(path.join(assets,'iphone-flat.png'),Buffer.from('synthetic test image'));fs.writeFileSync(path.join(assets,'target.png'),Buffer.from('different synthetic target'));fs.writeFileSync(path.join(assets,'trajectory.png'),Buffer.from('different synthetic trajectory'));
  const backend=new FixtureBackend(registry,{fixtureRoot:path.resolve('../../fixtures'),dataRoot,assetRoot:assets,delayMs,fault});
  t.after(()=>{backend.close();fs.rmSync(dataRoot,{recursive:true,force:true});});return {backend,dataRoot};
}
let sequence=0;
function request(method:string,params:Record<string,unknown>,op:string|null=null):RequestEnvelope{return {contract_version:'2.0.0',request_id:`req-${++sequence}`,operation_id:op,method,params,simulation:true};}
function createParams(id='iphone-flat',op='create-1'){
  return {operation_id:op,fixture_id:id,media_ref:`media/${id}.mp4`,user:{age_band:'adult',handedness:'right',target_stroke:'forehand',goal:'timing',exercise_background:'beginner',safety:'clear'},consent:{consent_id:'c1',task_version:1,purposes:['analysis','retest','diagnostics'],text_version:'v1',confirmed:true,guardian_simulated:false,recipient_digest:null,materials_digest:null},primary_route:'technical_diagnosis',auxiliary_routes:[],subject_id:null,mirror_confirmed:true};
}
const result=(r:ResponseEnvelope)=>{assert.equal(r.ok,true,JSON.stringify(r.error));return r.result as Record<string,unknown>;};
async function ready(backend:FixtureBackend,p=createParams()){
  const first=result(await backend.call(request('analysis.create',p,p.operation_id)));
  await new Promise(resolve=>setTimeout(resolve,20));return {task_id:first.task_id,version:1};
}

test('create is idempotent and conflicting reuse does not duplicate a task',async t=>{
  const {backend}=fixture(t);const p=createParams();const a=result(await backend.call(request('analysis.create',p,p.operation_id)));const b=result(await backend.call(request('analysis.create',p,p.operation_id)));
  assert.equal(a.task_id,b.task_id);const conflict=await backend.call(request('analysis.create',{...p,fixture_id:'gopro-flat'},p.operation_id));assert.equal(conflict.error?.code,'OPERATION_CONFLICT');
});
test('replayed creates show current state and ignore JSON object key order',async t=>{
  const {backend}=fixture(t);const p=createParams();const task=await ready(backend,p);
  const repeated=result(await backend.call(request('analysis.create',p,p.operation_id)));assert.equal(repeated.task_id,task.task_id);assert.equal(repeated.state,'complete');
  const reordered=Object.fromEntries(Object.entries(p).reverse());
  assert.equal(result(await backend.call(request('analysis.create',reordered,p.operation_id))).task_id,task.task_id);
});
test('youth consent is enforced and safety input does not create a training path',async t=>{
  const {backend}=fixture(t);const p=createParams();p.user.age_band='youth';assert.equal((await backend.call(request('analysis.create',p,p.operation_id))).error?.code,'GUARDIAN_CONFIRMATION_REQUIRED');
  p.consent.guardian_simulated=true;p.user.safety='pain';assert.equal(result(await backend.call(request('analysis.create',p,p.operation_id))).state,'safety_stop');
});
test('unsupported or unconfirmed subjects do not receive individual analysis',async t=>{
  const {backend}=fixture(t);for(const id of ['overlong','damaged','native-panorama']){const p=createParams(id,'op-'+id);assert.equal(result(await backend.call(request('analysis.create',p,p.operation_id))).state,'unsupported');}
  const p=createParams('multiple-subjects','multi');const task=result(await backend.call(request('analysis.create',p,p.operation_id)));assert.equal(task.state,'checking_input');assert.equal((await backend.call(request('analysis.metrics',{task_id:task.task_id,version:1}))).error?.code,'TASK_NOT_READY');
});
test('missing feet, ball and racket are represented as absent events, not invented observations',async t=>{
  const {backend}=fixture(t);for(const [id,event]of [['missing-feet','support'],['missing-ball','bounce_candidate'],['missing-racket','racket_drop']]){const task=await ready(backend,createParams(id!,'op-'+id));const timeline=result(await backend.call(request('analysis.timing',task)));const events=timeline.events as {kind:string;value_state:string;original_s:number|null}[];assert.equal(events.find(e=>e.kind===event)?.value_state,'not_visible');assert.equal(events.find(e=>e.kind===event)?.original_s,null);}
});
test('cancel remains responsive, preserves checkpoints, and never deletes',async t=>{
  const {backend,dataRoot}=fixture(t,200);const p=createParams();const task=result(await backend.call(request('analysis.create',p,p.operation_id)));const params={operation_id:'cancel-1',task_id:task.task_id,version:1};
  const cancelled=result(await backend.call(request('analysis.cancel',params,'cancel-1')));assert.equal(cancelled.state,'cancelled');assert.deepEqual(cancelled.checkpoints,['checking_input']);assert.ok(fs.existsSync(path.join(dataRoot,String(cancelled.workspace_ref))));
  assert.deepEqual(result(await backend.call(request('analysis.cancel',params,'cancel-1'))),cancelled);
});
test('resources, target and export stay structured, deletable and explicitly simulated',async t=>{
  const {backend,dataRoot}=fixture(t);const task=await ready(backend);const quality=result(await backend.call(request('analysis.quality',task)));const evidence=quality.evidence as {evidence_id:string}[];
  const overlay=result(await backend.call(request('evidence.render',{...task,operation_id:'render-1',evidence_ids:evidence.map(e=>e.evidence_id),mode:'frame'},'render-1')));const resources=overlay.resources as {resource_id:string;path:string;sha256:string}[];assert.ok(fs.existsSync(path.join(dataRoot,resources[0]!.path)));
  const target=result(await backend.call(request('target.generate',{...task,operation_id:'target-1',features:['balanced preparation']},'target-1')));assert.equal(target.kind,'target_pose');
  const trajectory=result(await backend.call(request('evidence.render',{...task,operation_id:'trajectory-1',evidence_ids:evidence.map(e=>e.evidence_id),mode:'trajectory'},'trajectory-1')));assert.notEqual((trajectory.resources as {sha256:string}[])[0]!.sha256,resources[0]!.sha256);assert.equal((trajectory.resources as {kind:string}[])[0]!.kind,'trajectory');assert.equal((trajectory.evidence as {resource:{sha256:string}}[])[0]!.resource.sha256,(trajectory.resources as {sha256:string}[])[0]!.sha256);assert.notEqual((trajectory.evidence as {evidence_id:string}[])[0]!.evidence_id,(overlay.evidence as {evidence_id:string}[])[0]!.evidence_id);
  const p=createParams();const exported=result(await backend.call(request('review.export',{...task,operation_id:'export-1',selected_resource_ids:[resources[0]!.resource_id],consent:p.consent},'export-1')));assert.equal(exported.redacted,true);
  const deleted=result(await backend.call(request('analysis.delete',{...task,operation_id:'delete-1',scope:'task',confirmed:true},'delete-1')));assert.equal(deleted.complete,true);assert.equal(deleted.runtime_uninstalled,false);assert.equal(deleted.external_copies_uncontrolled,true);assert.equal(result(await backend.call(request('analysis.status',task))).state,'deleted');
});
test('fixture faults include invalid responses and controlled process failure',async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-fault-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
  const bad=new FixtureBackend(registry,{fixtureRoot:path.resolve('../../fixtures'),dataRoot:root,fault:'invalid_response'});const r=await bad.call(request('analysis.status',{task_id:'t1',version:1}));assert.throws(()=>registry.response('get_analysis_status',r));
  const crash=new FixtureBackend(registry,{fixtureRoot:path.resolve('../../fixtures'),dataRoot:root,fault:'crash'});await assert.rejects(()=>crash.call(request('analysis.status',{task_id:'t1',version:1})),/SIMULATED_BACKEND_EXIT/);
});
test('evidence publishing ignores a legacy temp symlink and preserves the original',async t=>{
  const {backend,dataRoot}=fixture(t),task=await ready(backend),summary=result(await backend.call(request('analysis.status',task))),quality=result(await backend.call(request('analysis.quality',task)));
  const original=path.join(dataRoot,'user-original.txt'),temporary=path.join(dataRoot,String(summary.workspace_ref),'evidence/frame.png.tmp');fs.writeFileSync(original,'KEEP');fs.mkdirSync(path.dirname(temporary),{recursive:true});fs.symlinkSync(original,temporary);
  const output=result(await backend.call(request('evidence.render',{...task,operation_id:'safe-frame',evidence_ids:(quality.evidence as {evidence_id:string}[]).map(e=>e.evidence_id),mode:'frame'},'safe-frame')));assert.equal(fs.readFileSync(original,'utf8'),'KEEP');const file=path.join(dataRoot,(output.resources as {path:string}[])[0]!.path);assert.equal(fs.lstatSync(file).isSymbolicLink(),false);
});
test('partial deletion is explicit and timeout injection does not produce instant success',async t=>{
  const {backend}=fixture(t,5,'delete_partial');const task=await ready(backend);const deleted=result(await backend.call(request('analysis.delete',{...task,operation_id:'delete-partial',scope:'task',confirmed:true},'delete-partial')));assert.equal(deleted.complete,false);
  const {backend:slow}=fixture(t,5,'timeout');const pending=slow.call(request('analysis.status',{task_id:'missing',version:1}));
  assert.equal(await Promise.race([pending.then(()=>false),new Promise<boolean>(resolve=>setTimeout(()=>resolve(true),20))]),true);await pending;
});
test('session comparison gates relative change by matching conditions',async t=>{
  const {backend}=fixture(t);const a=await ready(backend,createParams('iphone-flat','compare-a'));const b=await ready(backend,createParams('iphone-flat','compare-b'));
  const same=result(await backend.call(request('sessions.compare',{...a,other_task_id:b.task_id,other_version:1})));assert.equal(same.comparable,true);assert.equal((same.changes as unknown[]).length,1);
  const c=await ready(backend,createParams('android-flat','compare-c'));const different=result(await backend.call(request('sessions.compare',{...a,other_task_id:c.task_id,other_version:1})));assert.equal(different.comparable,false);assert.deepEqual(different.changes,[]);
});
