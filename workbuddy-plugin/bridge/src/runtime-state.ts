import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {ContractRegistry,ContractError} from './contracts.js';
import {PathBoundary} from './paths.js';

export type RuntimeState='not_initialized'|'awaiting_consent'|'preparing'|'verifying'|'installed_stopped'|'starting'|'ready'|'repair_required'|'upgrading'|'uninstalling';
export interface RuntimeSnapshot {state:RuntimeState;version:string|null;backend:'fixture'|'python';checks:{code:string;passed:boolean}[];retained:string[];simulation:boolean;}
const transitions:Record<RuntimeState,RuntimeState[]>={
  not_initialized:['awaiting_consent'],awaiting_consent:['not_initialized','preparing','upgrading','installed_stopped','ready'],
  preparing:['verifying','repair_required','not_initialized'],verifying:['ready','repair_required'],
  installed_stopped:['starting','awaiting_consent','upgrading','uninstalling','repair_required'],
  starting:['verifying','repair_required','installed_stopped'],ready:['installed_stopped','awaiting_consent','upgrading','uninstalling','repair_required'],
  repair_required:['awaiting_consent','preparing','verifying','uninstalling'],upgrading:['verifying','ready','installed_stopped','repair_required'],
  uninstalling:['not_initialized','repair_required']
};

export class RuntimeStore {
  private current:RuntimeSnapshot;
  private readonly boundary:PathBoundary;
  readonly file:string;
  constructor(private readonly registry:ContractRegistry,root:string,mode:'fixture'|'python'){
    this.boundary=new PathBoundary(root,root);this.file=this.boundary.resolve('project',`runtime-${mode}.json`);
    this.current={state:'not_initialized',version:null,backend:mode,checks:[],retained:[],simulation:mode==='fixture'};
    if(fs.existsSync(this.file)){
      try{
        const saved=JSON.parse(fs.readFileSync(this.file,'utf8')) as RuntimeSnapshot;registry.output('runtime_status',saved);
        if(saved.backend!==mode||saved.simulation!==(mode==='fixture'))throw new Error('mode mismatch');
        this.current=saved;
        if(['preparing','starting','verifying','upgrading','uninstalling'].includes(saved.state))this.current={...saved,state:'repair_required',checks:[{code:'interrupted_operation',passed:false}]};
        else if(mode==='python'&&saved.state==='ready')this.current={...saved,state:'installed_stopped'};
      }catch{this.current={...this.current,state:'repair_required',checks:[{code:'state_corrupt',passed:false}]};}
    }
  }
  snapshot():RuntimeSnapshot{return structuredClone(this.current);}
  transition(state:RuntimeState,patch:Partial<Omit<RuntimeSnapshot,'state'|'backend'|'simulation'>>={}):RuntimeSnapshot {
    if(state!==this.current.state&&!transitions[this.current.state].includes(state))throw new ContractError('RUNTIME_TRANSITION_INVALID');
    const next={...this.current,...patch,state};
    if(state==='ready'&&(!next.version||!next.checks.length||next.checks.some(c=>!c.passed)))throw new ContractError('RUNTIME_VERIFICATION_REQUIRED');
    this.registry.output('runtime_status',next);
    const target=this.boundary.resolve('project',path.basename(this.file));fs.mkdirSync(path.dirname(target),{recursive:true});
    const temporary=target+'.'+randomUUID()+'.tmp';
    try{fs.writeFileSync(temporary,JSON.stringify(next,null,2)+'\n',{flag:'wx',mode:0o600});fs.renameSync(temporary,target);}catch(error){fs.rmSync(temporary,{force:true});throw error;}
    this.current=next;return this.snapshot();
  }
}
