import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {ContractRegistry,ContractError} from '../src/contracts.js';

const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
test('Bridge resolves all registered input and output schemas',()=>{
  assert.equal(registry.tools.length,19);assert.equal(registry.tool('analysis.cancel').name,'cancel_analysis');
  assert.equal(registry.tool('evidence.get').name,'get_evidence');
  assert.equal(registry.tool('analysis.estimate_storage').name,'estimate_storage');
  assert.throws(()=>registry.tool('delete_all_files'),ContractError);
});
test('V2 evidence inputs expose fixed backend limits and exactly one lookup mode',()=>{
  registry.input('get_evidence',{task_id:'task-1',version:1,candidate_id:'cand-1',include_zoom:true});
  registry.input('get_evidence',{task_id:'task-1',version:1,frame_start:10,frame_end:20});
  assert.throws(()=>registry.input('get_evidence',{task_id:'task-1',version:1,candidate_id:'cand-1',frame_start:10,frame_end:20}),/CONTRACT_INVALID/);
  assert.throws(()=>registry.input('get_evidence',{task_id:'task-1',version:1,candidate_id:'cand-1',side_frames:20}),/CONTRACT_INVALID/);
  assert.throws(()=>registry.input('get_evidence',{task_id:'task-1',version:1,frame_start:10,frame_end:20,frame_limit:200}),/CONTRACT_INVALID/);
  registry.input('estimate_storage',{task_id:'task-1',version:1,retain_mode:'full'});
  registry.input('create_video_analysis',{operation_id:'op-1',media_ref:'ingest/a.mp4',analysis_mode:'real_video',user:{age_band:'adult',handedness:'right',target_stroke:'forehand',goal:'review',exercise_background:'recreational',safety:'clear'},consent:{consent_id:'c-1',task_version:1,purposes:['analysis'],text_version:'v2',confirmed:true,guardian_simulated:false,recipient_digest:null,materials_digest:null},primary_route:'technical_diagnosis',auxiliary_routes:[],subject_id:null,mirror_confirmed:true,retain_mode:'full',detector:'coco'});
});
test('Bridge rejects mismatched side-effect operation IDs',()=>{
  const request={contract_version:'2.0.0',request_id:'r1',operation_id:'outer',method:'analysis.cancel',params:{operation_id:'inner',task_id:'t1',version:1},simulation:true};
  assert.throws(()=>registry.request(request),/OPERATION_ID_MISMATCH/);
  assert.equal(registry.request({...request,operation_id:'inner'}).operation_id,'inner');
});
test('contract errors contain pointers, not sensitive input',()=>{
  try{registry.input('get_analysis_status',{task_id:'private-secret',version:'invalid',private_chat:'private-secret'});assert.fail();}
  catch(e){assert.ok(e instanceof ContractError);assert.equal(e.code,'CONTRACT_INVALID');assert.ok(!JSON.stringify(e).includes('private-secret'));}
});
test('response simulation flag must match structured payload',()=>{
  const response={contract_version:'2.0.0',request_id:'r1',ok:true,result:{state:'ready',version:'0.1.0',backend:'fixture',checks:[],retained:[],simulation:true},error:null,simulation:true};
  assert.equal(registry.response('runtime_status',response).simulation,true);
  assert.throws(()=>registry.response('runtime_status',{...response,simulation:false}),/SIMULATION_MISMATCH/);
});
