import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {ContractRegistry,ContractError} from '../src/contracts.js';

const registry=new ContractRegistry(path.resolve('../../contracts/v1'));
test('Bridge resolves all registered input and output schemas',()=>{
  assert.equal(registry.tools.length,17);assert.equal(registry.tool('analysis.cancel').name,'cancel_analysis');
  assert.throws(()=>registry.tool('delete_all_files'),ContractError);
});
test('Bridge rejects mismatched side-effect operation IDs',()=>{
  const request={contract_version:'1.1.0',request_id:'r1',operation_id:'outer',method:'analysis.cancel',params:{operation_id:'inner',task_id:'t1',version:1},simulation:true};
  assert.throws(()=>registry.request(request),/OPERATION_ID_MISMATCH/);
  assert.equal(registry.request({...request,operation_id:'inner'}).operation_id,'inner');
});
test('contract errors contain pointers, not sensitive input',()=>{
  try{registry.input('get_analysis_status',{task_id:'private-secret',version:'invalid',private_chat:'private-secret'});assert.fail();}
  catch(e){assert.ok(e instanceof ContractError);assert.equal(e.code,'CONTRACT_INVALID');assert.ok(!JSON.stringify(e).includes('private-secret'));}
});
test('response simulation flag must match structured payload',()=>{
  const response={contract_version:'1.1.0',request_id:'r1',ok:true,result:{state:'ready',version:'0.1.0',backend:'fixture',checks:[],retained:[],simulation:true},error:null,simulation:true};
  assert.equal(registry.response('runtime_status',response).simulation,true);
  assert.throws(()=>registry.response('runtime_status',{...response,simulation:false}),/SIMULATION_MISMATCH/);
});
