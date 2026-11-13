import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import {VideoExperience} from '../src/video-experience.js';

const repoRoot=path.resolve('../..');
const metadata={filename:'my-video.mp4',age_band:'adult',guardian:false,handedness:'right',stroke:'forehand',goal:'Review this actual video',background:'Weekly practice',camera:'side',safety:'clear',consent:true,save:true,retest:true};
async function setup(t:{after:(fn:()=>unknown)=>unknown}){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-real-video-'));
  const experience=new VideoExperience(repoRoot,root);
  const server=http.createServer((req,res)=>{void experience.route(req,res,new URL(req.url||'/','http://127.0.0.1')).then(handled=>{if(!handled){res.writeHead(404);res.end();}});});
  await new Promise<void>(resolve=>server.listen(0,'127.0.0.1',resolve));
  const address=server.address();assert.ok(address&&typeof address==='object');
  t.after(async()=>{experience.close();await new Promise<void>(resolve=>server.close(()=>resolve()));fs.rmSync(root,{recursive:true,force:true});});
  return {url:`http://127.0.0.1:${address.port}`,root,experience};
}
async function upload(url:string,changes:Record<string,unknown>={},body:Buffer=Buffer.from('not real media')){return fetch(url+'/api/experience/videos',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Video-Metadata':Buffer.from(JSON.stringify({...metadata,...changes})).toString('base64url')},body:new Uint8Array(body)});}

test('real uploads enforce consent, guardian, safety and an actual nonempty stream',async t=>{
  const {url,root}=await setup(t);
  for(const [change,code] of [[{consent:false},'INVALID_INPUT'],[{age_band:'youth'},'GUARDIAN_REQUIRED'],[{safety:'pain'},'SAFETY_STOP'],[{filename:'panorama.insv'},'MEDIA_UNSUPPORTED']] as const){const response=await upload(url,change);assert.equal(response.status,422);assert.equal((await response.json() as any).error.code,code);}
  const empty=await upload(url,{},Buffer.alloc(0));assert.equal(empty.status,422);assert.deepEqual(fs.readdirSync(root),[]);
});

test('numbered local interface icons load successfully',async t=>{
  const {url}=await setup(t);for(const name of ['file-check-2','trash-2','git-compare-arrows']){const response=await fetch(url+'/icons/'+name+'.svg');assert.equal(response.status,200);assert.match(await response.text(),/<svg/);}
});

test('video tasks survive reload, resources stream ranges, and path escapes are rejected',async t=>{
  const {url,root}=await setup(t);const response=await upload(url);assert.equal(response.status,201);const job=await response.json() as any;assert.equal(job.simulation,false);assert.equal(fs.readFileSync(path.join(root,job.id,'source'),'utf8'),'not real media');
  const restored=new VideoExperience(repoRoot,root);assert.equal((restored.snapshot(job.id) as any).filename,metadata.filename);
  fs.writeFileSync(path.join(root,job.id,'preview.mp4'),'0123456789');
  const range=await fetch(url+`/api/experience/videos/${job.id}/resources/preview.mp4`,{headers:{Range:'bytes=2-5'}});assert.equal(range.status,206);assert.equal(range.headers.get('content-range'),'bytes 2-5/10');assert.equal(await range.text(),'2345');
  const source=await fetch(url+`/api/experience/videos/${job.id}/resources/source`);assert.equal(source.status,403);
  const external=path.join(root,'outside.json');fs.writeFileSync(external,'secret');fs.symlinkSync(external,path.join(root,job.id,'pose.json'));
  const symlink=await fetch(url+`/api/experience/videos/${job.id}/resources/pose.json`);assert.equal(symlink.status,404);
  const exported=await fetch(url+`/api/experience/videos/${job.id}/export`).then(response=>response.json()) as any;assert.equal(exported.pose,null);
});

test('stages require measured predecessors and cancellation preserves uploaded bytes',async t=>{
  const {url,root}=await setup(t);const job=await (await upload(url)).json() as any;const base=url+'/api/experience/videos/'+job.id;
  const pose=await fetch(base+'/pose',{method:'POST'});assert.equal(pose.status,422);assert.equal((await pose.json() as any).error.code,'INSPECTION_REQUIRED');
  const started=await fetch(base+'/inspect',{method:'POST'});assert.equal(started.status,202);
  const cancelled=await fetch(base+'/cancel',{method:'POST'});assert.equal(cancelled.status,200);assert.equal((await cancelled.json() as any).status,'cancelled');assert.equal(fs.readFileSync(path.join(root,job.id,'source'),'utf8'),'not real media');
});

test('non-retained video tasks are removed when the local service closes',async t=>{
  const {url,root,experience}=await setup(t);const job=await (await upload(url,{save:false})).json() as any;assert.equal(fs.existsSync(path.join(root,job.id,'source')),true);experience.close();assert.equal(fs.existsSync(path.join(root,job.id)),false);
});

test('deletion requires this task confirmation and removes the source and derived resources',async t=>{
  const {url,root}=await setup(t);const job=await (await upload(url)).json() as any;const route=url+'/api/experience/videos/'+job.id;
  const refused=await fetch(route,{method:'DELETE'});assert.equal(refused.status,422);assert.ok(fs.existsSync(path.join(root,job.id,'source')));
  const deleted=await fetch(route,{method:'DELETE',headers:{'X-Confirm-Video':job.id}});assert.equal(deleted.status,200);assert.equal((await deleted.json() as any).deleted,true);assert.equal(fs.existsSync(path.join(root,job.id)),false);
  assert.equal((await fetch(route)).status,404);
});
