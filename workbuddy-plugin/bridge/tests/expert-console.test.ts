import test from 'node:test';import assert from 'node:assert/strict';import path from 'node:path';
import {startPhase1Console} from '../src/phase1-console.js';
test('expert HTTP shows real capabilities, preview consent, bounded support and no role escalation',async()=>{
  const app=await startPhase1Console({repoRoot:path.resolve('../..'),port:0});
  try{
    const post=(route:string,value:unknown)=>fetch(app.url+'/api/experts/'+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
    const html=await fetch(app.url).then(r=>r.text());assert.match(html,/data-view="experts"/);assert.match(html,/id="view-experts"/);assert.match(html,/技术服务人员/);
    const script=await fetch(app.url+'/phase1.js').then(r=>r.text());assert.doesNotThrow(()=>new Function(script));assert.match(script,/MODEL_OUTPUT_TRUNCATED/);assert.match(script,/MODEL_OUTPUT_EMPTY/);assert.match(script,/重新预览资料并重试/);
    const config=await fetch(app.url+'/api/experts/config').then(r=>r.json()) as {roles:string[];supports:string[]};assert.equal(config.roles.length,7);assert.deepEqual(config.supports,['question_answer','video_evidence']);
    const preview=await post('preview',{question:'正手怎么准备？'}).then(r=>r.json()) as {preview_id:string;media:unknown[]};assert.deepEqual(preview.media,[]);
    assert.equal((await post('start',{preview_id:preview.preview_id,confirmed:false})).status,403);
    assert.equal((await post('runtime/repair',{role:'tennis-runtime-support',shell:'kill'})).status,403);
    assert.equal((await post('runtime/config',{control_timeout_ms:15000,file:'../code.ts'})).status,403);
    assert.equal((await post('runtime/config',{control_timeout_ms:10000})).status,200);
    const health=await fetch(app.url+'/api/experts/runtime').then(r=>r.json()) as {status:string;task_completion:string};assert.equal(health.status,'stopped');assert.equal(health.task_completion,'not_checked');
    assert.equal((await post('runtime/repair',{})).status,409);
    assert.equal((await fetch(app.url+'/api/experts/start',{method:'POST',headers:{Origin:'https://example.com'},body:'{}'})).status,403);
  }finally{await app.close();}
});
