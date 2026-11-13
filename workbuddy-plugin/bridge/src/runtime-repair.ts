import {ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';
export interface RepairInput {operation_id:string;consent_confirmed:true;scope_digest:string;}
export interface RepairDependencies {
  authorizedScope:string;
  repairArtifact:(version:string|null)=>Promise<{version:string;checks:{code:string;passed:boolean}[]}>;
}
const repairs=new WeakMap<RuntimeStore,Map<string,{scope:string;promise:Promise<RuntimeSnapshot>}>>();
export function repairRuntime(registry:ContractRegistry,store:RuntimeStore,input:RepairInput,deps:RepairDependencies):Promise<RuntimeSnapshot>{
  registry.input('runtime_repair',input);
  if(input.scope_digest!==deps.authorizedScope)return Promise.reject(new ContractError('SCOPE_CONFIRMATION_REQUIRED'));
  const saved=repairs.get(store)||new Map();repairs.set(store,saved);
  const prior=saved.get(input.operation_id);if(prior)return prior.scope===input.scope_digest?prior.promise.then(()=>store.snapshot()):Promise.reject(new ContractError('OPERATION_CONFLICT'));
  const job=(async()=>{
    const before=store.snapshot();if(before.state!=='repair_required')throw new ContractError('REPAIR_NOT_REQUIRED');
    store.transition('preparing');
    try{
      const repaired=await deps.repairArtifact(before.version);store.transition('verifying',{checks:repaired.checks});
      if(!repaired.checks.length||repaired.checks.some(c=>!c.passed))throw new ContractError('REPAIR_VERIFICATION_FAILED');
      return store.transition('ready',{version:repaired.version,checks:repaired.checks,retained:['runtime_components','original_request']});
    }catch(error){store.transition('repair_required',{checks:[{code:'repair_failed',passed:false}],retained:[...new Set([...before.retained,'original_request'])]});throw error instanceof ContractError?error:new ContractError('REPAIR_FAILED');}
  })();
  saved.set(input.operation_id,{scope:input.scope_digest,promise:job});void job.catch(()=>saved.delete(input.operation_id));return job;
}
