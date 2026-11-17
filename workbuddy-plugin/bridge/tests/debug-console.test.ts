import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {startDebugConsole} from '../src/debug-console.js';

const repoRoot=path.resolve('../..');
function tempRoot(t:{after:(fn:()=>unknown)=>unknown}):string {const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-debug-console-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return root;}
async function post(url:string,route:string,payload:unknown={}):Promise<{status:number;body:any}>{const response=await fetch(url+route,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});return {status:response.status,body:await response.json()};}
async function getJson(url:string,route:string):Promise<{status:number;body:any}>{const response=await fetch(url+route);return {status:response.status,body:await response.json()};}
let operation=0;
function createParams(fixture='iphone-flat'){
  operation+=1;
  return {operation_id:`console-create-${operation}`,fixture_id:fixture,media_ref:`media/${fixture}.mp4`,user:{age_band:'adult',handedness:'right',target_stroke:'forehand',goal:'story test',exercise_background:'fixture only',safety:'clear'},consent:{consent_id:`consent-${operation}`,task_version:1,purposes:['analysis','retest','diagnostics'],text_version:'v1',confirmed:true,guardian_simulated:false,recipient_digest:null,materials_digest:null},primary_route:'technical_diagnosis',auxiliary_routes:[],subject_id:null,mirror_confirmed:true};
}
async function tool(url:string,name:string,payload:unknown):Promise<{status:number;body:any}>{return post(url,`/api/tools/${name}`,payload);}

test('console refuses non-loopback binding and serves a secured local shell',async t=>{
  await assert.rejects(()=>startDebugConsole({repoRoot,host:'0.0.0.0',port:0,dataRoot:tempRoot(t)}),/LOOPBACK_ONLY/);
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const page=await fetch(handle.url);assert.equal(page.status,200);assert.match(await page.text(),/网球视频分析工作台/);assert.match(page.headers.get('content-security-policy')||'',/default-src 'self'/);
  const debug=await fetch(handle.url+'/debug');assert.match(await debug.text(),/网球用户故事测试工作台/);
  const session=await fetch(handle.url+'/api/session').then(response=>response.json()) as {lifecycle:string;backend:{running:boolean};mode:string};
  assert.equal(session.lifecycle,'stopped');assert.equal(session.backend.running,false);assert.equal(session.mode,'python');
});

test('Host and Origin checks reject drive-by browser requests',async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const address=handle.server.address();assert.ok(address&&typeof address==='object');
  const status=await new Promise<number>((resolve,reject)=>{const request=http.request({host:'127.0.0.1',port:address.port,path:'/api/session',headers:{Host:'attacker.example',Origin:'https://attacker.example'}},response=>{response.resume();resolve(response.statusCode||0);});request.on('error',reject);request.end();});
  assert.equal(status,403);
});

test('fixture lifecycle switches to ready, exposes simulation and stops cleanly',async t=>{
  const dataRoot=tempRoot(t);const handle=await startDebugConsole({repoRoot,port:0,dataRoot});t.after(()=>handle.close());
  const started=await post(handle.url,'/api/session/start',{mode:'fixture'});assert.equal(started.status,200);assert.equal(started.body.lifecycle,'ready');assert.equal(started.body.backend.running,true);assert.equal(started.body.backend.simulation,true);assert.equal(started.body.backend.checks.every((item:{passed:boolean})=>item.passed),true);
  assert.equal(fs.realpathSync(started.body.data_root).startsWith(fs.realpathSync(dataRoot)),true);
  const stopped=await post(handle.url,'/api/session/stop');assert.equal(stopped.body.lifecycle,'stopped');assert.equal(stopped.body.backend.running,false);assert.equal(stopped.body.backend.state,'installed_stopped');
});

test('Python lifecycle performs a real stdio health round-trip with 13 checks', {timeout:30000}, async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const started=await post(handle.url,'/api/session/start',{mode:'python'});assert.equal(started.status,200);assert.equal(started.body.lifecycle,'ready',JSON.stringify(started.body.last_error));assert.equal(started.body.backend.running,true);assert.equal(started.body.backend.simulation,false);assert.equal(started.body.backend.checks.length,13);assert.equal(started.body.backend.checks.every((item:{passed:boolean})=>item.passed),true);
  const restarted=await post(handle.url,'/api/session/restart',{mode:'python'});assert.equal(restarted.body.lifecycle,'ready');assert.equal(restarted.body.backend.running,true);
  const stopped=await post(handle.url,'/api/session/stop');assert.equal(stopped.body.lifecycle,'stopped');assert.equal(stopped.body.backend.running,false);
});

test('capability API keeps measured, simulated and unavailable boundaries separate',async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const capabilities=await fetch(handle.url+'/api/capabilities').then(response=>response.json()) as {measured:{id:string}[];simulated:{id:string}[];unavailable:{id:string}[]};
  assert.equal(capabilities.measured.some(item=>item.id==='runtime-health'),true);assert.equal(capabilities.simulated.some(item=>item.id==='fixture-analysis'),true);assert.equal(capabilities.unavailable.some(item=>item.id==='real-pose'),true);
});

test('story catalog and dynamic tool registry are complete and traceable',async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const stories=await getJson(handle.url,'/api/stories');assert.equal(stories.status,200);assert.equal(stories.body.stories.length,41);assert.equal(stories.body.scenarios.length,9);assert.match(stories.body.source,/用户故事-V02\.md$/);
  assert.equal(stories.body.stories.filter((item:{coverage:string})=>item.coverage==='executable').length,20);
  assert.equal(stories.body.stories.filter((item:{coverage:string})=>item.coverage==='partial').length,7);
  assert.equal(stories.body.stories.filter((item:{coverage:string})=>item.coverage==='external').length,14);
  const tools=await getJson(handle.url,'/api/tools');assert.equal(tools.status,200);assert.equal(tools.body.items.length,19);assert.equal(new Set(tools.body.items.map((item:{name:string})=>item.name)).size,19);assert.ok(tools.body.items.every((item:{inputSchema:unknown})=>item.inputSchema));
  const fixtures=await getJson(handle.url,'/api/fixtures');assert.equal(fixtures.body.items.some((item:{fixture_id:string})=>item.fixture_id==='multiple-subjects'),true);
});

test('tool endpoint preserves contract pointers and bounded redacted history',async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  const invalid=await tool(handle.url,'get_analysis_status',{task_id:'task-safe',version:'private-value',private_chat:'/Users/private/video.mp4'});
  assert.equal(invalid.status,422);assert.equal(invalid.body.error.code,'CONTRACT_INVALID');assert.ok(invalid.body.error.pointers.includes('/version'));assert.equal(JSON.stringify(invalid.body).includes('private-value'),false);assert.equal(JSON.stringify(invalid.body).includes('/Users/private'),false);
  const history=await getJson(handle.url,'/api/history');assert.equal(history.body.limit,200);assert.equal(history.body.items.length,1);assert.equal(history.body.items[0].task_id,'task-safe');assert.equal(JSON.stringify(history.body).includes('private_chat'),false);
});

test('fixture workbench creates, polls, renders, previews and deletes a task safely',{timeout:15000},async t=>{
  const dataRoot=tempRoot(t);const handle=await startDebugConsole({repoRoot,port:0,dataRoot});t.after(()=>handle.close());
  assert.equal((await post(handle.url,'/api/session/start',{mode:'fixture'})).body.lifecycle,'ready');
  const created=await tool(handle.url,'create_video_analysis',createParams());assert.equal(created.status,200);assert.equal(created.body.response.ok,true);const task=created.body.response.result as {task_id:string;version:number;state:string};assert.equal(task.state,'analyzing');
  let status=task;
  for(let index=0;index<10&&status.state==='analyzing';index+=1){await new Promise(resolve=>setTimeout(resolve,120));const polled=await tool(handle.url,'get_analysis_status',{task_id:task.task_id,version:task.version});status=polled.body.response.result;}
  assert.equal(status.state,'complete');
  const quality=await tool(handle.url,'get_quality_report',{task_id:task.task_id,version:task.version});const evidence=quality.body.response.result.evidence as {evidence_id:string}[];assert.ok(evidence.length>0);
  const rendered=await tool(handle.url,'render_evidence_overlay',{operation_id:`render-${++operation}`,task_id:task.task_id,version:task.version,evidence_ids:evidence.map(item=>item.evidence_id),mode:'frame'});assert.equal(rendered.body.response.ok,true);const resource=rendered.body.response.result.resources[0] as {path:string;mime:string;resource_id:string};
  const preview=await fetch(handle.url+'/api/resource?path='+encodeURIComponent(resource.path));assert.equal(preview.status,200);assert.equal(preview.headers.get('content-type'),resource.mime);assert.ok((await preview.arrayBuffer()).byteLength>0);
  const exported=await tool(handle.url,'export_review_package',{operation_id:`export-${++operation}`,task_id:task.task_id,version:task.version,selected_resource_ids:[resource.resource_id],consent:createParams().consent});assert.equal(exported.body.response.ok,true);const manifest=exported.body.response.result.manifest as {path:string;mime:string;resource_id:string};assert.equal(manifest.mime,'application/json');
  const manifestPreview=await fetch(handle.url+'/api/resource?path='+encodeURIComponent(manifest.path));assert.equal(manifestPreview.status,200);assert.equal(manifestPreview.headers.get('content-type'),'application/json');const manifestBody=await manifestPreview.json() as {simulation:boolean;selected:{resource_id:string}[]};assert.equal(manifestBody.simulation,true);assert.equal(manifestBody.selected[0]?.resource_id,resource.resource_id);
  const tasks=await getJson(handle.url,'/api/tasks');assert.equal(tasks.body.tasks.find((item:{task_id:string})=>item.task_id===task.task_id).fixture_id,'iphone-flat');assert.equal(tasks.body.resources.some((item:{resource_id:string})=>item.resource_id===resource.resource_id),true);
  const outside=path.join(dataRoot,'outside.png');fs.writeFileSync(outside,'outside');const absolute=path.join(dataRoot,'fixture','public',resource.path);fs.rmSync(absolute);fs.symlinkSync(outside,absolute);
  const escaped=await fetch(handle.url+'/api/resource?path='+encodeURIComponent(resource.path));assert.equal(escaped.status,422);const escapedBody=await escaped.json() as {error:{code:string}};assert.equal(escapedBody.error.code,'PATH_FORBIDDEN');
  const deleted=await tool(handle.url,'delete_analysis_data',{operation_id:`delete-${++operation}`,task_id:task.task_id,version:task.version,scope:'task',confirmed:true});assert.equal(deleted.body.response.result.complete,true);
  const after=await getJson(handle.url,'/api/tasks');assert.equal(after.body.tasks.find((item:{task_id:string})=>item.task_id===task.task_id).state,'deleted');assert.equal(after.body.resources.some((item:{task_id:string})=>item.task_id===task.task_id),false);
});

test('fixture fault selection restarts fixture and reports controlled failures',async t=>{
  const handle=await startDebugConsole({repoRoot,port:0,dataRoot:tempRoot(t)});t.after(()=>handle.close());
  await post(handle.url,'/api/session/start',{mode:'fixture'});
  const changed=await post(handle.url,'/api/session/fault',{fault:'crash'});assert.equal(changed.status,200);assert.equal(changed.body.fault,'crash');assert.equal(changed.body.session.lifecycle,'ready');
  const failed=await tool(handle.url,'get_analysis_status',{task_id:'task-missing',version:1});assert.equal(failed.status,200);assert.equal(failed.body.response.ok,false);assert.equal(failed.body.response.error.code,'BRIDGE_OPERATION_FAILED');
  const invalid=await post(handle.url,'/api/session/fault',{fault:'filesystem-corruption'});assert.equal(invalid.status,422);assert.equal(invalid.body.error.code,'FIXTURE_FAULT_INVALID');
});
