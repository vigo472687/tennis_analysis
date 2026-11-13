import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import {startPhase1Console} from '../src/phase1-console.js';import {createFixtureParams,envelope} from '../src/phase1-backend.js';
test('Phase 2 HTTP uploads actual bytes, exercises real probe, validates mode/path, and keeps control responsive',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'wb2-http-')),app=await startPhase1Console({repoRoot:path.resolve('../..'),port:0});
 const post=(route:string,value:unknown)=>fetch(app.url+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
 const upload=(filename:string,bytes:string,origin?:string)=>fetch(app.url+'/api/phase2/upload',{method:'POST',headers:{'X-File-Name':encodeURIComponent(filename),...(origin?{Origin:origin}:{})},body:bytes});
 const call=async(p:any)=>{const line=JSON.stringify(envelope('analysis.create',p,true,p.operation_id));return (await post('/api/phase1/request',{line}).then(r=>r.json()) as any).response;};
 const params=(op:string,ref:string)=>{const p:any=createFixtureParams(op);delete p.fixture_id;p.analysis_mode='real_video';p.media_ref=ref;p.consent.guardian_simulated=false;return p;};
 try{
  await post('/api/phase1/start',{data_root:root});assert.equal((await upload('bad.mp4','x','https://example.org')).status,403);assert.equal((await upload('../bad.mp4','x')).status,400);assert.equal((await upload('bad.txt','x')).status,400);
  const u=await upload('broken.mp4','INVALID_VIDEO').then(r=>r.json()) as any;assert.equal(fs.readFileSync(path.join(root,u.media_ref),'utf8'),'INVALID_VIDEO');
  const create=call(params('op-corrupt',u.media_ref));let done=false;create.then(()=>done=true);
  const health=await app.backend.request(JSON.stringify(envelope('runtime.status',{runtime_requirement:'not_required'},false)));assert.equal(health.response.ok,true);assert.equal(done,false);
  const response=await create;assert.equal(response.ok,true);assert.equal(response.result.state,'unsupported');assert.deepEqual(response.result.recovery_actions,['reshoot']);
  const replay=await call(params('op-corrupt',u.media_ref));assert.equal(replay.result.task_id,response.result.task_id);
  assert.equal((await call({...params('op-invalid',u.media_ref),fixture_id:'iphone-flat'})).error.code,'REQUEST_PARAMS_INVALID');
  assert.equal((await call(params('op-path','../x.mp4'))).error.code,'REQUEST_PARAMS_INVALID');assert.equal((await call(params('op-missing','ingest/missing.mp4'))).error.code,'INGEST_VIDEO_MISSING');
  assert.equal((await fetch(app.url+'/api/phase2/artifacts?workspace_ref=../x')).status,400);assert.equal((await fetch(app.url+'/phase2/artifact?workspace_ref=projects/task-none/v1&kind=video')).status,404);
 }finally{await app.close();fs.rmSync(root,{recursive:true,force:true});}
});
