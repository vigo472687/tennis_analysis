import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';
import {startPhase1Console} from '../src/phase1-console.js';
test('candidate HTTP is scoped, legacy reads degrade, invalid reselect has explicit error, render subject is validated',async()=>{
 const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'subjects-http-'))),w='projects/task-old/v1',records=root+'/public/'+w+'/records';fs.mkdirSync(records,{recursive:true});fs.writeFileSync(records+'/task-config.json',JSON.stringify({analysis_mode:'real_video',subject_id:null,media_ref:'ingest/test.mp4'}));fs.mkdirSync(root+'/ingest');fs.writeFileSync(root+'/ingest/test.mp4','test');
 const h=await startPhase1Console({repoRoot:path.resolve('../..'),port:0});await h.backend.start(root);const get=async(route:string)=>{const r=await fetch(h.url+route);return {status:r.status,value:await r.json() as any};},post=async(route:string,body:any)=>{const r=await fetch(h.url+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return {status:r.status,value:await r.json() as any};};
 try{const old=await get('/api/phase5/subjects?workspace_ref='+w);assert.equal(old.status,200);assert.equal(old.value.legacy,true);assert.deepEqual(old.value.subjects,[]);assert.equal((await get('/api/phase5/subjects?workspace_ref=../ingest')).status,400);
 fs.writeFileSync(records+'/subject-candidates.json',JSON.stringify({requested:null,mode:'unconfirmed',analyzed:[],subject_stability:'multi',candidates:[{subject_id:'track-0',frames:10,share:1,persistent:true,first_s:0,last_s:1,size_ratio:1}]}));
 const invalid=await post('/api/phase5/reselect',{workspace_ref:w,subject_id:'track-99',confirmed:true});assert.equal(invalid.status,400);assert.equal(invalid.value.error.code,'SUBJECT_ID_INVALID');assert.equal((await get('/api/phase5/active')).value,null);
 const absentConsent=await post('/api/phase5/reselect',{workspace_ref:w,subject_id:'track-0',confirmed:false});assert.equal(absentConsent.value.error.code,'ANALYSIS_CONSENT_REQUIRED');
 const render=await post('/api/phase4/render',{workspace_ref:w,kind:'overlay',subject_id:'track-99'});assert.equal(render.value.error.code,'SUBJECT_ID_INVALID');
 const js=await (await fetch(h.url+'/phase1.js')).text();assert.ok(js.includes('p5-subject-mode'));new Function(js);
 }finally{await h.close();fs.rmSync(root,{recursive:true,force:true});}
});
