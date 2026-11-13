import {ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';

export interface StartInput {operation_id:string;runtime_requirement:'required';}
export interface StartDependencies {isRunning:()=>boolean;startBackend:()=>Promise<void>;verifyBackend:()=>Promise<{code:string;passed:boolean}[]>;}
const starting=new WeakMap<RuntimeStore,Promise<RuntimeSnapshot>>();

export function startRuntime(registry:ContractRegistry,store:RuntimeStore,input:StartInput,deps:StartDependencies):Promise<RuntimeSnapshot>{
  registry.input('runtime_start',input);
  const pending=starting.get(store);if(pending)return pending;
  const job=(async()=>{
    let before=store.snapshot();
    if(before.state==='ready'&&deps.isRunning())return before;
    if(before.state==='ready'){store.transition('installed_stopped');before=store.snapshot();}
    if(before.state!=='installed_stopped'||!before.version)throw new ContractError('RUNTIME_NOT_PREPARED');
    store.transition('starting');
    try{
      await deps.startBackend();const checks=await deps.verifyBackend();store.transition('verifying',{checks});
      if(!checks.length||checks.some(c=>!c.passed))throw new ContractError('BACKEND_HEALTH_FAILED');
      return store.transition('ready',{checks});
    }catch(error){store.transition('repair_required',{checks:[{code:'backend_start_failed',passed:false}]});throw error instanceof ContractError?error:new ContractError('BACKEND_START_FAILED');}
  })();
  starting.set(store,job);void job.finally(()=>starting.delete(store)).catch(()=>undefined);return job;
}

interface OriginalOperation {fingerprint:string;run:()=>Promise<unknown>;result?:Promise<unknown>;cancelled:boolean;}
export class OriginalRequestQueue {
  private readonly operations=new Map<string,OriginalOperation>();
  retain(operationId:string,fingerprint:string,run:()=>Promise<unknown>):void {
    const prior=this.operations.get(operationId);
    if(prior&&prior.fingerprint!==fingerprint)throw new ContractError('OPERATION_CONFLICT');
    if(!prior)this.operations.set(operationId,{fingerprint,run,cancelled:false});
  }
  resume(operationId:string):Promise<unknown>{
    const item=this.operations.get(operationId);if(!item)return Promise.reject(new ContractError('ORIGINAL_REQUEST_MISSING'));
    if(item.cancelled)return Promise.reject(new ContractError('OPERATION_CANCELLED'));
    item.result??=Promise.resolve().then(()=>item.run());return item.result;
  }
  cancel(operationId:string):void {const item=this.operations.get(operationId);if(item&&!item.result)item.cancelled=true;}
  retryFailed(operationId:string):void {const item=this.operations.get(operationId);if(item?.result)void item.result.catch(()=>{item.result=undefined;});}
}
