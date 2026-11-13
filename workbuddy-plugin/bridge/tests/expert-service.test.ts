import test from 'node:test';import assert from 'node:assert/strict';import path from 'node:path';
import {ExpertService} from '../src/expert-service.js';import {ExpertSources} from '../src/expert-sources.js';
import {ExpertError} from '../src/expert-policy.js';
const root=path.resolve('../..');
function model(answer:Record<string,unknown>,delay=false){let calls=0;return {status:()=>({configured:true,provider:'test',model:'deepseek-flash',base_url:'test'}),models:async()=>({provider:'test',models:[],selected:'deepseek-flash'}),chat:async()=>{calls++;if(delay)await new Promise(r=>setTimeout(r,40));return {result:answer,usage:null};},calls:()=>calls};}
const answer={answer:'一般知识；没有个人视频，不能诊断。',source_ids:[],limits:['来源未核验'],missing_inputs:[],route:'question_answer',next_action:'如需个人分析，请提供对应视频。'};
const sources=()=>new ExpertSources(root,async()=>new Response('',{status:503}));
test('preview requires explicit confirmation; duplicate start coalesces; unknown user fields never default',async()=>{
  const client=model(answer),service=new ExpertService(root,client,sources());const p=service.preview({question:'如何练正手？'});assert.equal(p.input.handedness,'unknown');assert.deepEqual(p.media,[]);
  assert.throws(()=>service.start(p.preview_id,false),/EXPERT_SEND_CONFIRMATION_REQUIRED/);const run=service.start(p.preview_id,true);assert.equal(service.start(p.preview_id,true).id,run.id);
  while(service.get(run.id).status==='running')await new Promise(r=>setTimeout(r,5));assert.equal(service.get(run.id).status,'completed');assert.equal(client.calls(),2);
  assert.throws(()=>service.preview({question:'test',role:'tennis-runtime-support'}),/EXPERT_INPUT_FIELD_DENIED/);
});
test('cancelled runs suppress late model results; invented citations fail instead of publishing',async()=>{
  const client=model(answer,true),service=new ExpertService(root,client,sources());const p=service.preview({question:'test'});const run=service.start(p.preview_id,true);service.cancel(run.id);await new Promise(r=>setTimeout(r,80));assert.equal(service.get(run.id).status,'cancelled');assert.equal(service.get(run.id).result,null);
  const bad=new ExpertService(root,model({...answer,source_ids:['INVENTED']}),sources());const bp=bad.preview({question:'test'}),br=bad.start(bp.preview_id,true);while(bad.get(br.id).status==='running')await new Promise(r=>setTimeout(r,5));assert.equal(bad.get(br.id).error,'EXPERT_SOURCE_INVALID');
});
test('reported discomfort stops before any model call or prescribed load',()=>{
  const client=model(answer),service=new ExpertService(root,client,sources());const p=service.preview({question:'还能加量吗',safety:'discomfort'});
  assert.throws(()=>service.start(p.preview_id,true),/EXPERT_SAFETY_STOP/);assert.equal(client.calls(),0);
});
test('empty or malformed result is repaired without rerunning a completed member',async()=>{
  let calls=0;const client={...model(answer),chat:async()=>{calls++;if(calls===2)throw new ExpertError('MODEL_OUTPUT_EMPTY',502);return {result:answer,usage:null};}};
  const service=new ExpertService(root,client,sources()),p=service.preview({question:'正手准备怎么练'}),run=service.start(p.preview_id,true);
  while(service.get(run.id).status==='running')await new Promise(r=>setTimeout(r,5));
  const result=service.get(run.id);assert.equal(result.status,'completed');assert.equal(result.calls,3);assert.equal(result.format_repairs,1);assert.equal(calls,3);
});
test('format repair is limited to two extra calls across the entire run',async()=>{
  let calls=0;const client={...model(answer),chat:async()=>{calls++;throw new ExpertError('MODEL_JSON_INVALID',502);}};
  const service=new ExpertService(root,client,sources()),p=service.preview({question:'test'}),run=service.start(p.preview_id,true);
  while(service.get(run.id).status==='running')await new Promise(r=>setTimeout(r,5));const result=service.get(run.id);assert.equal(result.status,'failed');assert.equal(result.calls,3);assert.equal(result.format_repairs,2);assert.equal(calls,3);
});
test('transport failures, truncation and invented sources never trigger format retries',async()=>{
  for(const code of ['MODEL_CONNECTION_FAILED','MODEL_RATE_LIMITED','MODEL_OUTPUT_TRUNCATED','EXPERT_SOURCE_INVALID']){
    let calls=0;const client={...model(answer),chat:async()=>{calls++;throw new ExpertError(code,502);}};
    const service=new ExpertService(root,client,sources()),p=service.preview({question:'test'}),run=service.start(p.preview_id,true);
    while(service.get(run.id).status==='running')await new Promise(r=>setTimeout(r,5));assert.equal(service.get(run.id).error,code);assert.equal(calls,1);
  }
});
test('format repair budget is shared between members',async()=>{
  let calls=0;const client={...model(answer),chat:async()=>{calls++;if(calls!==2)throw new ExpertError('MODEL_JSON_INVALID',502);return {result:answer,usage:null};}};
  const service=new ExpertService(root,client,sources()),p=service.preview({question:'test'}),run=service.start(p.preview_id,true);
  while(service.get(run.id).status==='running')await new Promise(r=>setTimeout(r,5));const result=service.get(run.id);assert.equal(result.status,'failed');assert.equal(result.format_repairs,2);assert.equal(calls,4);
  assert.equal(result.members.find(m=>m.role==='tennis-research-trainer')?.status,'completed');
});
test('cancellation during format repair prevents late publication and the next member',async()=>{
  let calls=0,release!:(value:{result:typeof answer;usage:null})=>void,started!:()=>void;
  const retryStarted=new Promise<void>(resolve=>{started=resolve;});
  const client={...model(answer),chat:async()=>{calls++;if(calls===1)throw new ExpertError('MODEL_OUTPUT_EMPTY',502);started();return new Promise<{result:typeof answer;usage:null}>(resolve=>{release=resolve;});}};
  const service=new ExpertService(root,client,sources()),p=service.preview({question:'test'}),run=service.start(p.preview_id,true);
  await retryStarted;service.cancel(run.id);release({result:answer,usage:null});await new Promise(r=>setTimeout(r,10));
  const result=service.get(run.id);assert.equal(result.status,'cancelled');assert.equal(result.result,null);assert.equal(calls,2);
});
