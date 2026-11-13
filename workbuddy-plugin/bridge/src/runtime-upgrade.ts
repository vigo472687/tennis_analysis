import {ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';
import type {PrepareInput} from './runtime-prepare.js';
export interface UpgradeDependencies {
  authorizedScope:string;
  verifyTarget:(version:string)=>Promise<{code:string;passed:boolean}[]>;
  restorePrevious:(version:string)=>Promise<boolean>;
}
const operations=new WeakMap<RuntimeStore,Map<string,{fingerprint:string;promise:Promise<RuntimeSnapshot>}>>();
export function changeRuntimeVersion(registry:ContractRegistry,store:RuntimeStore,input:PrepareInput,deps:UpgradeDependencies):Promise<RuntimeSnapshot>{
  registry.input('runtime_prepare',input);
  if(input.intent==='prepare')return Promise.reject(new ContractError('RUNTIME_INTENT_UNSUPPORTED'));
  if(input.scope_digest!==deps.authorizedScope)return Promise.reject(new ContractError('SCOPE_CONFIRMATION_REQUIRED'));
  const saved=operations.get(store)||new Map();operations.set(store,saved);const fingerprint=JSON.stringify([input.intent,input.target_version,input.scope_digest]);
  const prior=saved.get(input.operation_id);if(prior)return prior.fingerprint===fingerprint?prior.promise.then(()=>store.snapshot()):Promise.reject(new ContractError('OPERATION_CONFLICT'));
  const job=(async()=>{
    const before=store.snapshot();if(!before.version||!['ready','installed_stopped'].includes(before.state))throw new ContractError('RUNTIME_NOT_PREPARED');
    if(before.version===input.target_version)return before;
    store.transition('upgrading');
    try{
      const checks=await deps.verifyTarget(input.target_version);
      if(!checks.length||checks.some(c=>!c.passed))throw new ContractError('ARTIFACT_VERIFICATION_FAILED');
      store.transition('verifying',{checks});return store.transition('ready',{version:input.target_version,checks,retained:['runtime_components','previous_version','project_data']});
    }catch(error){
      let restored=false;try{restored=await deps.restorePrevious(before.version);}catch{/* Failure is represented in runtime state. */}
      if(restored)store.transition(before.state==='ready'?'ready':'installed_stopped',{version:before.version,checks:before.checks,retained:['runtime_components','project_data']});
      else store.transition('repair_required',{version:before.version,checks:[{code:'rollback_failed',passed:false}],retained:['previous_version','project_data']});
      throw error instanceof ContractError?error:new ContractError('VERSION_CHANGE_FAILED');
    }
  })();saved.set(input.operation_id,{fingerprint,promise:job});void job.catch(()=>saved.delete(input.operation_id));return job;
}
