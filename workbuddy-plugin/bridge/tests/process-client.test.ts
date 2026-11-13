import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {ProcessClient} from '../src/process-client.js';
import {ContractRegistry,type RequestEnvelope} from '../src/contracts.js';
import {SafeLogger} from '../src/logging.js';

const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
const script=`let buffer='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>{buffer+=chunk;let at;while((at=buffer.indexOf('\\n'))>=0){const r=JSON.parse(buffer.slice(0,at));buffer=buffer.slice(at+1);if(r.params.task_id==='bad'){process.stdout.write('not-json\\n');continue;}if(r.params.task_id==='exit'){process.exit(2);}const state=r.method==='analysis.cancel'?'cancelled':'complete';const result={task_id:r.params.task_id,version:1,state,stage:'none',checkpoints:[],retained:process.env.TENNIS_TEST_PARENT_SECRET?['SECRET_FORWARDED']:[],recovery_actions:['none'],workspace_ref:'projects/'+r.params.task_id+'/v1',simulation:true};const reply=()=>process.stdout.write(JSON.stringify({contract_version:'1.1.0',request_id:r.request_id,ok:true,result,error:null,simulation:true})+'\\n');if(r.params.task_id==='slow')setTimeout(reply,600);else reply();}});`;
let id=0;const request=(task:string):RequestEnvelope=>({contract_version:'1.1.0',request_id:`req-${++id}`,operation_id:null,method:'analysis.status',params:{task_id:task,version:1},simulation:true});
function client(t:{after:(fn:()=>Promise<void>)=>unknown},timeoutMs=1000){const logs:string[]=[];const c=new ProcessClient(registry,{command:process.execPath,args:['-e',script],cwd:process.cwd(),timeoutMs},new SafeLogger(line=>logs.push(line)));t.after(()=>c.close());return {c,logs};}

test('constructor does not spawn; explicit start reuses one process and supports concurrent replies',async t=>{
  const {c}=client(t);assert.equal(c.running,false);await assert.rejects(()=>c.call(request('fast')),/RUNTIME_NOT_READY/);await Promise.all([c.start(),c.start()]);assert.equal(c.running,true);
  const order:string[]=[];const slow=c.call(request('slow')).then(()=>order.push('slow'));const fast=c.call(request('fast')).then(()=>order.push('fast'));await Promise.all([slow,fast]);assert.deepEqual(order,['fast','slow']);
});
test('timeout does not block status/cancel and does not automatically replay',async t=>{
  const {c}=client(t,300);await c.start();const slow=c.call(request('slow'));const response=await c.call(request('fast'));assert.equal(response.ok,true);await assert.rejects(()=>slow,/BACKEND_TIMEOUT/);assert.equal(c.running,true);
  const cancel:RequestEnvelope={contract_version:'1.1.0',request_id:`req-${++id}`,operation_id:'cancel-1',method:'analysis.cancel',params:{operation_id:'cancel-1',task_id:'fast',version:1},simulation:true};assert.equal((await c.call(cancel)).ok,true);
});
test('invalid stdout and process exit become bounded contract errors',async t=>{
  const {c}=client(t);await c.start();await assert.rejects(()=>c.call(request('bad')),/BACKEND_PROTOCOL_INVALID/);
  const {c:exits}=client(t);await exits.start();await assert.rejects(()=>exits.call(request('exit')),/BACKEND_EXIT/);
});
test('request IDs cannot be reused and arbitrary shell executables are rejected',async t=>{
  assert.throws(()=>new ProcessClient(registry,{command:'sh',args:['-c','anything'],cwd:process.cwd()}),/EXECUTABLE_FORBIDDEN/);
  assert.doesNotThrow(()=>new ProcessClient(registry,{command:process.execPath,args:['-e',''],cwd:process.cwd()}));
  const {c}=client(t);await c.start();const r=request('fast');await c.call(r);await assert.rejects(()=>c.call(r),/REQUEST_ID_REUSED/);
});
test('parent provider credentials are not forwarded to the media backend',async t=>{
  process.env.TENNIS_TEST_PARENT_SECRET='private-provider-secret';t.after(()=>{delete process.env.TENNIS_TEST_PARENT_SECRET;});
  const {c}=client(t);await c.start();const response=await c.call(request('fast'));assert.equal(response.ok,true);assert.deepEqual((response.result as {retained:string[]}).retained,[]);
});
