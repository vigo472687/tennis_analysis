import fs from 'node:fs';import path from 'node:path';import {randomUUID} from 'node:crypto';
import {ExpertError} from './expert-policy.js';
export class ExpertStore {
  constructor(private readonly configuredRoot:()=>string|null){}
  private root(){const root=this.configuredRoot();if(!root)throw new ExpertError('DATA_ROOT_REQUIRED',409);return fs.realpathSync(root);}
  private base(workspace:string){
    if(!/^projects\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/v[1-9]\d*$/.test(workspace))throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    const root=this.root(),base=path.join(root,'public',workspace);
    const file=path.join(base,'records/task-config.json');if(!fs.existsSync(file)||fs.realpathSync(file)!==file)throw new ExpertError('EXPERT_CONTEXT_ARTIFACT_MISSING',409);
    return base;
  }
  save(workspace:string,run:Record<string,any>){
    if(!/^expert-[a-f0-9-]{36}$/.test(run.id))throw new ExpertError('EXPERT_RUN_INVALID');
    const dir=path.join(this.base(workspace),'expert');if(!fs.existsSync(dir))fs.mkdirSync(dir,{mode:0o700});if(fs.realpathSync(dir)!==dir)throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    const file=path.join(dir,run.id+'.json');if(fs.existsSync(file)&&fs.realpathSync(file)!==file)throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    const payload=JSON.stringify(run);if(payload.length>1024*1024)throw new ExpertError('EXPERT_RECORD_TOO_LARGE',413);
    const temp=path.join(dir,'.'+randomUUID()+'.tmp');try{fs.writeFileSync(temp,payload,{mode:0o600,flag:'wx'});fs.renameSync(temp,file);}finally{if(fs.existsSync(temp))fs.unlinkSync(temp);}
  }
  load(id:string):Record<string,any>|null {
    if(!/^expert-[a-f0-9-]{36}$/.test(id))throw new ExpertError('EXPERT_RUN_NOT_FOUND',404);
    const root=this.root(),projects=path.join(root,'public/projects');if(!fs.existsSync(projects))return null;if(fs.realpathSync(projects)!==projects)throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    let count=0;for(const task of fs.readdirSync(projects,{withFileTypes:true})){
      if(!task.isDirectory()||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(task.name))continue;
      const taskDir=path.join(projects,task.name);if(fs.realpathSync(taskDir)!==taskDir)continue;
      for(const version of fs.readdirSync(taskDir,{withFileTypes:true})){
        if(!version.isDirectory()||!/^v[1-9]\d*$/.test(version.name))continue;if(++count>2000)throw new ExpertError('EXPERT_RECORD_CAPACITY');
        const workspace='projects/'+task.name+'/'+version.name,file=path.join(taskDir,version.name,'expert',id+'.json');if(!fs.existsSync(file))continue;
        this.base(workspace);if(fs.realpathSync(file)!==file||!fs.statSync(file).isFile()||fs.statSync(file).size>1024*1024)throw new ExpertError('EXPERT_RECORD_INVALID');
        let run:any;try{run=JSON.parse(fs.readFileSync(file,'utf8'));}catch{throw new ExpertError('EXPERT_RECORD_INVALID');}
        if(run.id!==id||run.workspace_ref!==workspace||!Array.isArray(run.members)||!['running','completed','failed','cancelled'].includes(run.status))throw new ExpertError('EXPERT_RECORD_INVALID');
        if(run.status==='running'){
          run.status='failed';run.error='EXPERT_INTERRUPTED_UNKNOWN';run.stage='运行中断，远端请求结果未知';run.result=null;run.ended_at=new Date().toISOString();
          for(const member of run.members)if(member.status==='running')member.status='unknown';
          this.save(workspace,run);
        }
        return run;
      }
    }return null;
  }
}
