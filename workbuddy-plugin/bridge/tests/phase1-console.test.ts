import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {startPhase1Console} from '../src/phase1-console.js';
import {envelope} from '../src/phase1-backend.js';

test('Phase 1 HTTP console controls an actual process and forwards malformed protocol without prevalidation',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'phase1-http-'));
  const app=await startPhase1Console({repoRoot:path.resolve('../..'),port:0});
  const request=(route:string,value:unknown,headers:Record<string,string>={})=>fetch(app.url+route,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(value)});
  try{
    assert.equal((await fetch(app.url+'/api/phase1/state').then(r=>r.json()) as any).running,false);
    assert.equal((await request('/api/phase1/start',{data_root:root},{Origin:'https://example.org'})).status,403);
    const html=await fetch(app.url+'/');assert.equal(html.status,200);assert.match(html.headers.get('content-security-policy')||'',/frame-ancestors 'none'/);assert.match(await html.text(),/Phase 1/);
    const script=await fetch(app.url+'/phase1.js').then(r=>r.text());assert.doesNotThrow(()=>new Function(script));
    assert.equal((await fetch(app.url+'/icons/terminal.svg')).status,200);
    const started=await request('/api/phase1/start',{data_root:root}).then(r=>r.json()) as any;assert.equal(started.running,true);assert.equal(started.health.checks.length,13);
    const broken=await request('/api/phase1/request',{line:'{broken'}).then(r=>r.json()) as any;assert.equal(broken.response.error.code,'TRANSPORT_INVALID_JSON');
    const health=await request('/api/phase1/request',{line:JSON.stringify(envelope('runtime.status',{runtime_requirement:'not_required'},false))}).then(r=>r.json()) as any;
    assert.equal(health.response.ok,true);assert.equal(health.response.simulation,false);
    assert.equal((await request('/api/phase1/request',{line:'{}\n{}'})).status,400);
    const validation=await fetch(app.url+'/api/phase1/validation').then(r=>r.json()) as any;
    assert.deepEqual(validation.items.map((x:any)=>x.id),['sample','empty']);
    if(validation.items[0].summary)assert.equal(validation.items[0].summary.stage_a_detection.frames_analysed,900);
    const media=validation.items[0].files['skeleton.mp4'];if(media){const range=await fetch(app.url+media,{headers:{Range:'bytes=0-63'}});assert.equal(range.status,206);assert.equal((await range.arrayBuffer()).byteLength,64);}
    assert.equal((await fetch(app.url+'/validation/sample/other.mp4')).status,404);
    const stopped=await request('/api/phase1/stop',{}).then(r=>r.json()) as any;assert.equal(stopped.running,false);
  }finally{await app.close();fs.rmSync(root,{recursive:true,force:true});}
});

test('Phase 1 rejects a public listener',async()=>{
  await assert.rejects(()=>startPhase1Console({repoRoot:path.resolve('../..'),port:0,host:'0.0.0.0'}),/LOOPBACK_ONLY/);
});
