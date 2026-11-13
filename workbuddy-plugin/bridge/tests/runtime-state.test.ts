import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {ContractRegistry} from '../src/contracts.js';
import {RuntimeStore} from '../src/runtime-state.js';

const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
function setup(t:{after:(f:()=>void)=>unknown}){const root=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-runtime-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));return root;}
test('loading an unused runtime is read-only and snapshots cannot mutate authority',t=>{
  const root=setup(t);const store=new RuntimeStore(registry,root,'fixture');assert.equal(store.snapshot().state,'not_initialized');assert.equal(fs.existsSync(store.file),false);
  const snapshot=store.snapshot();snapshot.state='ready';assert.equal(store.snapshot().state,'not_initialized');
  assert.throws(()=>store.transition('ready'),/RUNTIME_TRANSITION_INVALID/);
});
test('ready requires verified checks and state persists atomically',t=>{
  const root=setup(t);const store=new RuntimeStore(registry,root,'fixture');store.transition('awaiting_consent');store.transition('preparing');store.transition('verifying');
  assert.throws(()=>store.transition('ready',{version:'0.1.0',checks:[]}),/RUNTIME_VERIFICATION_REQUIRED/);
  store.transition('ready',{version:'0.1.0',checks:[{code:'contract_ok',passed:true}]});
  assert.equal(new RuntimeStore(registry,root,'fixture').snapshot().state,'ready');assert.equal(fs.existsSync(store.file+'.tmp'),false);
});
test('interrupted preparation and corrupt state require explicit repair',t=>{
  const root=setup(t);const store=new RuntimeStore(registry,root,'fixture');store.transition('awaiting_consent');store.transition('preparing');
  assert.equal(new RuntimeStore(registry,root,'fixture').snapshot().state,'repair_required');
  fs.writeFileSync(store.file,'broken');const broken=new RuntimeStore(registry,root,'fixture');assert.equal(broken.snapshot().state,'repair_required');assert.equal(fs.readFileSync(store.file,'utf8'),'broken');
});
test('reloaded Python readiness means installed but not a running process',t=>{
  const root=setup(t);const store=new RuntimeStore(registry,root,'python');store.transition('awaiting_consent');store.transition('preparing');store.transition('verifying');store.transition('ready',{version:'0.1.0',checks:[{code:'health_ok',passed:true}]});
  assert.equal(new RuntimeStore(registry,root,'python').snapshot().state,'installed_stopped');assert.equal(new RuntimeStore(registry,root,'fixture').snapshot().state,'not_initialized');
});
test('a preexisting legacy temporary symlink cannot overwrite an unrelated original',t=>{
  const root=setup(t),store=new RuntimeStore(registry,root,'fixture'),original=path.join(root,'user-original.txt');fs.writeFileSync(original,'KEEP');fs.symlinkSync(original,store.file+'.tmp');
  store.transition('awaiting_consent');assert.equal(fs.readFileSync(original,'utf8'),'KEEP');assert.equal(JSON.parse(fs.readFileSync(store.file,'utf8')).state,'awaiting_consent');assert.equal(fs.lstatSync(store.file+'.tmp').isSymbolicLink(),true);
});
