import test from 'node:test';
import assert from 'node:assert/strict';
import {ExpertPolicy,EXPERT_ROLES} from '../src/expert-policy.js';
test('business roles cannot mutate runtime; spoofed identities and other policies are rejected',()=>{
  const policy=new ExpertPolicy();
  for(const role of EXPERT_ROLES.filter(r=>r!=='tennis-runtime-support')){
    const actor=policy.bind(role);policy.require(actor,'business.read');
    for(const action of ['runtime.inspect','runtime.restart','runtime.configure'] as const)assert.throws(()=>policy.require(actor,action),/EXPERT_ACTION_DENIED/);
  }
  assert.throws(()=>policy.require({role:'tennis-runtime-support'},'runtime.restart'),/EXPERT_IDENTITY_DENIED/);
  assert.throws(()=>policy.require(new ExpertPolicy().bind('tennis-runtime-support'),'runtime.restart'),/EXPERT_IDENTITY_DENIED/);
  policy.require(policy.bind('tennis-runtime-support'),'runtime.restart');
});
test('runtime config permits a bounded control timeout, never arbitrary environment or files',()=>{
  const policy=new ExpertPolicy();assert.deepEqual(policy.configuration({control_timeout_ms:15000}),{control_timeout_ms:15000});
  for(const value of [{control_timeout_ms:1},{control_timeout_ms:Infinity},{control_timeout_ms:15000,file:'../agents/a.md'},{PATH:'/tmp'},{shell:'rm'},null])assert.throws(()=>policy.configuration(value));
});
