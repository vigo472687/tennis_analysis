import test from 'node:test';
import assert from 'node:assert/strict';
import {ExpertPolicy} from '../src/expert-policy.js';
import {ExpertRuntime} from '../src/expert-runtime.js';
import type {Phase1Backend} from '../src/phase1-backend.js';
test('busy jobs are not probed or restarted and actor spoofing is denied',async()=>{
  const policy=new ExpertPolicy(),actor=policy.bind('tennis-runtime-support');let starts=0;
  const backend={snapshot:()=>({running:true,pending:[{method:'analysis.create'}]}),start:async()=>{starts++;},request:async()=>{throw new Error('should not probe');}} as unknown as Phase1Backend;
  const runtime=new ExpertRuntime(backend,policy);assert.equal((await runtime.inspect(actor)).status,'busy');await runtime.repair(actor);assert.equal(starts,0);
  await assert.rejects(async()=>runtime.repair({role:'tennis-runtime-support'}),/EXPERT_IDENTITY_DENIED/);
});
test('only the existing owned backend root can be recovered, coalescing repeated requests',async()=>{
  const policy=new ExpertPolicy(),actor=policy.bind('tennis-runtime-support');let running=false,starts=0;
  const backend={snapshot:()=>({running,pending:[],data_root:'/owned/project'}),start:async(root:string)=>{assert.equal(root,'/owned/project');starts++;running=true;},request:async()=>({response:{ok:true,result:{checks:[{code:'ok',passed:true}]}}})} as unknown as Phase1Backend;
  const runtime=new ExpertRuntime(backend,policy);await Promise.all([runtime.repair(actor),runtime.repair(actor)]);assert.equal(starts,1);assert.equal((await runtime.inspect(actor)).status,'ready');
  assert.equal(runtime.configure(actor,{control_timeout_ms:5000}).scope,'support_health_probe');
  assert.throws(()=>runtime.configure(actor,{file:'../business.ts',control_timeout_ms:5000}),/RUNTIME_CONFIG_DENIED/);
});
test('a missing backend package is never reported as present',async()=>{
  const policy=new ExpertPolicy(),actor=policy.bind('tennis-runtime-support');
  const backend={repoRoot:'/nonexistent-tennis-runtime',snapshot:()=>({running:false,pending:[]})} as unknown as Phase1Backend;
  assert.equal((await new ExpertRuntime(backend,policy).inspect(actor)).component_present,false);
});
