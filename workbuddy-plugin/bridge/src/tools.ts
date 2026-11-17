import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {CONTRACT_VERSION,ContractRegistry,ContractError,type JsonObject,type RequestEnvelope,type ResponseEnvelope} from './contracts.js';
import {RuntimeStore} from './runtime-state.js';
import {prepareRuntime,type PrepareDependencies,type PrepareInput} from './runtime-prepare.js';
import {startRuntime,OriginalRequestQueue,type StartDependencies,type StartInput} from './runtime-start.js';
import {repairRuntime,type RepairDependencies,type RepairInput} from './runtime-repair.js';
import {changeRuntimeVersion,type UpgradeDependencies} from './runtime-upgrade.js';
import {uninstallRuntime,type UninstallDependencies,type UninstallInput} from './runtime-uninstall.js';
export interface DispatcherOptions {
  contractRoot:string;store:RuntimeStore;simulation:boolean;authorizedScope:string;
  backend:{call:(r:RequestEnvelope)=>Promise<ResponseEnvelope>};
  prepare:PrepareDependencies;start:StartDependencies;repair:RepairDependencies;upgrade:UpgradeDependencies;uninstall:UninstallDependencies;
}
function canonical(value:unknown):string {if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>JSON.stringify(k)+':'+canonical(v)).join(',')+'}';return JSON.stringify(value);}
export class ToolDispatcher {
  private readonly queue=new OriginalRequestQueue();
  constructor(readonly registry:ContractRegistry,private readonly options:DispatcherOptions){}
  definitions():{name:string;description:string;inputSchema:JsonObject;annotations:{readOnlyHint:boolean;destructiveHint:boolean;idempotentHint:boolean;openWorldHint:boolean}}[]{
    const expand=(value:unknown,base:string):unknown=>{
      if(Array.isArray(value))return value.map(v=>expand(v,base));
      if(!value||typeof value!=='object')return value;
      const obj=value as JsonObject;
      if(typeof obj.$ref==='string'){
        const url=new URL(obj.$ref,base);if(url.origin!=='https://tennis.local'||!/^\/contracts\/v1\/[a-z-]+\.schema\.json$/.test(url.pathname))throw new ContractError('CONTRACT_REFERENCE_FORBIDDEN');
        const document=JSON.parse(fs.readFileSync(path.join(this.options.contractRoot,path.basename(url.pathname)),'utf8')) as JsonObject;
        const target=url.hash.slice(2).split('/').reduce<unknown>((p,k)=>(p as JsonObject)[k],document);return expand(target,url.href.split('#')[0]!);
      }
      return Object.fromEntries(Object.entries(obj).filter(([k])=>!['$schema','$id'].includes(k)).map(([k,v])=>[k,expand(v,base)]));
    };
    return this.registry.tools.map(t=>({name:t.name,description:`${t.method}; ${t.side_effect?'Requires explicit operation_id and purpose confirmation.':'Read-only.'} Prototype analysis is simulated.`,inputSchema:expand({$ref:this.registry.toolRef(t,'input')},'https://tennis.local/contracts/v1/common.schema.json') as JsonObject,annotations:{readOnlyHint:!t.side_effect,destructiveHint:['runtime_uninstall','delete_analysis_data'].includes(t.name),idempotentHint:true,openWorldHint:false}}));
  }
  async call(name:string,params:unknown):Promise<ResponseEnvelope>{
    const requestId='req-'+randomUUID();let simulation=this.options.simulation;
    try{
      const tool=this.registry.tool(name);this.registry.input(tool.name,params);const input=params as JsonObject;
      if(tool.method.startsWith('runtime.')){
        let result;
        if(tool.name==='runtime_status')result=this.options.store.snapshot();
        else if(tool.name==='runtime_prepare'){
          if(input.scope_digest!==this.options.authorizedScope)throw new ContractError('SCOPE_CONFIRMATION_REQUIRED');
          result=input.intent==='prepare'?await prepareRuntime(this.registry,this.options.store,input as unknown as PrepareInput,this.options.prepare):await changeRuntimeVersion(this.registry,this.options.store,input as unknown as PrepareInput,this.options.upgrade);
        }else if(tool.name==='runtime_start')result=await startRuntime(this.registry,this.options.store,input as unknown as StartInput,this.options.start);
        else if(tool.name==='runtime_repair')result=await repairRuntime(this.registry,this.options.store,input as unknown as RepairInput,this.options.repair);
        else result=await uninstallRuntime(this.registry,this.options.store,input as unknown as UninstallInput,this.options.uninstall);
        simulation=result.simulation;return this.registry.response(tool.name,{contract_version:CONTRACT_VERSION,request_id:requestId,ok:true,result,error:null,simulation});
      }
      const state=this.options.store.snapshot().state;
      if(state!=='ready'||!this.options.start.isRunning())throw new ContractError(state==='not_initialized'?'RUNTIME_PREPARATION_REQUIRED':'RUNTIME_START_REQUIRED');
      const request:RequestEnvelope={contract_version:CONTRACT_VERSION,request_id:requestId,operation_id:tool.side_effect?String(input.operation_id):null,method:tool.method,params:input,simulation};this.registry.request(request);
      let response:ResponseEnvelope;
      if(tool.side_effect){const id=String(input.operation_id);this.queue.retain(id,canonical([tool.method,input]),()=>this.options.backend.call(request));response=await this.queue.resume(id) as ResponseEnvelope;}
      else response=await this.options.backend.call(request);
      return this.registry.response(tool.name,{...response,request_id:requestId});
    }catch(error){
      const code=error instanceof ContractError?error.code:'BRIDGE_OPERATION_FAILED';
      const runtime=code.startsWith('RUNTIME')||code.includes('REPAIR')||code.includes('ARTIFACT');
      const result:ResponseEnvelope={contract_version:CONTRACT_VERSION,request_id:requestId,ok:false,result:null,error:{code,stage:runtime?'runtime':'input',retryable:false,user_action:runtime?'repair':'confirm',retained:[],details_ref:null,simulation},simulation};this.registry.validate('common','response',result);return result;
    }
  }
}
