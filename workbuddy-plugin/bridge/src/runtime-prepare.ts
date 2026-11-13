import {ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';

export interface PrepareInput {operation_id:string;consent_confirmed:true;target_version:string;intent:'prepare'|'upgrade'|'rollback';scope_digest:string;}
export interface PrepareDependencies {
  checkSpace:()=>boolean;
  verifyArtifact:(version:string,signal?:AbortSignal)=>Promise<{code:string;passed:boolean}[]>;
}
interface Operation {fingerprint:string;promise:Promise<RuntimeSnapshot>;}
const operations=new WeakMap<RuntimeStore,Map<string,Operation>>();
const active=new WeakMap<RuntimeStore,Operation>();

export function prepareRuntime(registry:ContractRegistry,store:RuntimeStore,input:PrepareInput,deps:PrepareDependencies,signal?:AbortSignal):Promise<RuntimeSnapshot>{
  registry.input('runtime_prepare',input);
  if(input.intent!=='prepare')return Promise.reject(new ContractError('RUNTIME_INTENT_UNSUPPORTED'));
  if(signal?.aborted)return Promise.reject(new ContractError('OPERATION_CANCELLED'));
  const fingerprint=JSON.stringify([input.intent,input.target_version,input.scope_digest]);
  const saved=operations.get(store)||new Map<string,Operation>();operations.set(store,saved);
  const prior=saved.get(input.operation_id);
  if(prior){if(prior.fingerprint!==fingerprint)return Promise.reject(new ContractError('OPERATION_CONFLICT'));return prior.promise.then(()=>store.snapshot());}
  const running=active.get(store);
  if(running){if(running.fingerprint!==fingerprint)return Promise.reject(new ContractError('RUNTIME_BUSY'));return running.promise;}
  const job=(async()=>{
    const before=store.snapshot();
    if(before.version===input.target_version&&['ready','installed_stopped'].includes(before.state))return before;
    if(!['not_initialized','repair_required'].includes(before.state))throw new ContractError('RUNTIME_BUSY');
    if(!deps.checkSpace())throw new ContractError('STORAGE_UNAVAILABLE');
    store.transition('awaiting_consent');store.transition('preparing');
    try{
      const checks=await deps.verifyArtifact(input.target_version,signal);
      if(signal?.aborted)throw new ContractError('OPERATION_CANCELLED');
      store.transition('verifying',{checks});
      if(!checks.length||checks.some(c=>!c.passed))throw new ContractError('ARTIFACT_VERIFICATION_FAILED');
      return store.transition('ready',{version:input.target_version,checks,retained:['runtime_components']});
    }catch(error){
      store.transition('repair_required',{checks:[{code:error instanceof ContractError?error.code:'PREPARATION_FAILED',passed:false}],retained:['partial_components']});
      throw error instanceof ContractError?error:new ContractError('PREPARATION_FAILED');
    }
  })();
  const operation={fingerprint,promise:job};saved.set(input.operation_id,operation);active.set(store,operation);
  void job.finally(()=>active.delete(store)).catch(()=>{saved.delete(input.operation_id);});
  return job;
}
