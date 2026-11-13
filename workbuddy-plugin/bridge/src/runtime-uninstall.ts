import {ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';
export interface UninstallInput {operation_id:string;confirmed:true;keep_project_data:boolean;}
export interface RemovalItem {kind:'components'|'project_data';removed:boolean;}
export interface UninstallDependencies {
  projectDeletionConfirmed:boolean;
  stopBackend:()=>Promise<void>;
  removeComponents:()=>Promise<RemovalItem[]>;
  deleteProjects:()=>Promise<RemovalItem[]>;
}
const operations=new WeakMap<RuntimeStore,Map<string,{keep:boolean;promise:Promise<RuntimeSnapshot>}>>();
export function uninstallRuntime(registry:ContractRegistry,store:RuntimeStore,input:UninstallInput,deps:UninstallDependencies):Promise<RuntimeSnapshot>{
  registry.input('runtime_uninstall',input);
  if(!input.keep_project_data&&!deps.projectDeletionConfirmed)return Promise.reject(new ContractError('PROJECT_DELETION_CONFIRMATION_REQUIRED'));
  const saved=operations.get(store)||new Map();operations.set(store,saved);const prior=saved.get(input.operation_id);
  if(prior)return prior.keep===input.keep_project_data?prior.promise.then(()=>store.snapshot()):Promise.reject(new ContractError('OPERATION_CONFLICT'));
  const job=(async()=>{
    const before=store.snapshot();if(before.state==='not_initialized')return before;
    if(!['ready','installed_stopped','repair_required'].includes(before.state))throw new ContractError('RUNTIME_BUSY');
    store.transition('uninstalling');
    const retained=new Set<string>(['external_copies']);if(input.keep_project_data)retained.add('project_data');
    const items:RemovalItem[]=[];
    try{
      await deps.stopBackend();items.push(...await deps.removeComponents());
      if(!input.keep_project_data)items.push(...await deps.deleteProjects());
      for(const item of items)if(!item.removed)retained.add(item.kind);
      if(items.some(i=>!i.removed)||!items.some(i=>i.kind==='components')||(!input.keep_project_data&&!items.some(i=>i.kind==='project_data'))){retained.add('unconfirmed_removal');return store.transition('repair_required',{checks:[{code:'removal_partial',passed:false}],retained:[...retained]});}
      return store.transition('not_initialized',{version:null,checks:[{code:'components_removed',passed:true}],retained:[...retained]});
    }catch{retained.add('unconfirmed_removal');return store.transition('repair_required',{checks:[{code:'removal_failed',passed:false}],retained:[...retained]});}
  })();saved.set(input.operation_id,{keep:input.keep_project_data,promise:job});return job;
}
