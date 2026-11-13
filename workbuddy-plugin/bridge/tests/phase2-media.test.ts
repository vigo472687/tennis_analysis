import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import os from 'node:os';import path from 'node:path';import {Readable} from 'node:stream';import {Phase2Media} from '../src/phase2-media.js';
test('Phase 2 ingestion preserves uploads, avoids overwrites, and rejects unsafe names and ingest symlinks',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'wb2-media-'));const store=new Phase2Media(path.resolve('../..'),()=>root);try{
  const a=await store.upload(Readable.from(['abc']),'测试.mov'),b=await store.upload(Readable.from(['def']),'测试.mov');assert.equal(a.filename,'测试.mov');assert.match(a.media_ref,/^ingest\/[A-Za-z0-9_.-]+\.mov$/);assert.notEqual(a.media_ref,b.media_ref);assert.equal(fs.readFileSync(path.join(root,a.media_ref),'utf8'),'abc');
  for(const name of ['../x.mp4','x.txt','a\\b.mp4'])await assert.rejects(()=>store.upload(Readable.from(['x']),name));
  await assert.rejects(()=>store.upload(Readable.from([]),'empty.mp4'),/UPLOAD_EMPTY/);assert.ok(!fs.readdirSync(path.join(root,'ingest')).some(x=>x.endsWith('.part')));
  fs.renameSync(path.join(root,'ingest'),path.join(root,'saved'));fs.symlinkSync(path.join(root,'saved'),path.join(root,'ingest'));
  await assert.rejects(()=>store.upload(Readable.from(['x']),'x.mp4'),/MEDIA_REF_OUTSIDE_ROOT/);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
test('Phase 2 result statistics use actual records, preserve lost frames and restrict artifact paths',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'wb2-result-'));const store=new Phase2Media(path.resolve('../..'),()=>root),workspace='projects/task-test/v1',base=path.join(root,'public',workspace);try{
  fs.mkdirSync(path.join(base,'records'),{recursive:true});fs.mkdirSync(path.join(base,'subjects'));fs.writeFileSync(path.join(base,'records/task-config.json'),'{"analysis_mode":"real_video"}');fs.writeFileSync(path.join(base,'subjects/subjects-annotated.mp4'),'video');
  fs.writeFileSync(path.join(base,'records/subjects-frames.jsonl'),[{frame:0,pts_s:0,boxes:[{}],track_id:1,lost:false},{frame:1,pts_s:0.1,boxes:[],track_id:null,lost:true}].map(x=>JSON.stringify(x)).join('\n'));
  const r=store.result(workspace);assert.equal(r.summary.frames,2);assert.equal(r.summary.retention,0.5);assert.equal(r.frames[1].track_id,null);
  assert.throws(()=>store.artifact('../x','video'));assert.throws(()=>store.artifact(workspace,'other'));fs.unlinkSync(path.join(base,'subjects/subjects-annotated.mp4'));fs.symlinkSync(path.join(base,'records/task-config.json'),path.join(base,'subjects/subjects-annotated.mp4'));assert.throws(()=>store.artifact(workspace,'video'),/MEDIA_REF_OUTSIDE_ROOT/);
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('Chinese/space upload is accepted by the real creation adapter without widening path validation',async()=>{
 const {Phase5Service}=await import('../src/phase5-service.js'),{ContractRegistry}=await import('../src/contracts.js');
 const root=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'supplement-name-')));let requested:any;
 try{const uploaded=await new Phase2Media(path.resolve('../..'),()=>root).upload(Readable.from(['original-video']),'补充 正手.mp4');
 const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
 const service=new Phase5Service(()=>root,registry,async line=>{requested=JSON.parse(line);throw Error('isolated-no-inference');});
 const job=service.start({media_ref:uploaded.media_ref,confirmed:true});assert.equal(job.pending,true);assert.equal(requested.params.media_ref,uploaded.media_ref);assert.equal(fs.readFileSync(path.join(root,uploaded.media_ref),'utf8'),'original-video');assert.equal(uploaded.filename,'补充 正手.mp4');
 await new Promise(resolve=>setImmediate(resolve));assert.equal(service.job(job.id).error,'isolated-no-inference');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
});
