import fs from 'node:fs';
import path from 'node:path';
import {envelope,type Phase1Backend} from './phase1-backend.js';
import {ExpertError,ExpertPolicy,type ExecutorIdentity} from './expert-policy.js';
type Backend=Pick<Phase1Backend,'snapshot'|'start'|'request'>&Partial<Pick<Phase1Backend,'repoRoot'>>;
export class ExpertRuntime {
  private timeout=15000;
  private recovering:Promise<unknown>|null=null;
  private attempts=0;
  private readonly events:{at:string;action:string;result:string}[]=[];
  constructor(private readonly backend:Backend,private readonly policy:ExpertPolicy){}
  configure(actor:ExecutorIdentity,value:unknown){this.policy.require(actor,'runtime.configure');this.timeout=this.policy.configuration(value).control_timeout_ms;return {control_timeout_ms:this.timeout,scope:'support_health_probe',persistent:false};}
  private event(action:string,result:string){this.events.push({at:new Date().toISOString(),action,result});if(this.events.length>20)this.events.shift();}
  async inspect(actor:ExecutorIdentity){
    this.policy.require(actor,'runtime.inspect');const snapshot=this.backend.snapshot();
    const base={component_present:this.backend.repoRoot?fs.existsSync(path.join(this.backend.repoRoot,'local-backend/pyproject.toml')):null,process_running:snapshot.running,protocol:'not_checked' as string,capability:'not_checked' as string,task_completion:'not_checked',pending_methods:snapshot.pending.map(p=>p.method),repair_attempts:this.attempts,events:[...this.events]};
    if(!snapshot.running)return {...base,status:'stopped',next_action:'运行环境未启动或已退出；可检查并恢复。'};
    if(snapshot.pending.length)return {...base,status:'busy',next_action:'后端存在活动请求，保留当前处理，不重启。'};
    let timer:NodeJS.Timeout|undefined;
    try{
      const response=await Promise.race([this.backend.request(JSON.stringify(envelope('runtime.status',{runtime_requirement:'not_required'},false))),new Promise<never>((_resolve,reject)=>{timer=setTimeout(()=>reject(new Error('HEALTH_PENDING')),this.timeout);})]);
      const checks=response.response.result?.checks;const passed=response.response.ok&&Array.isArray(checks)&&checks.length>0&&checks.every(c=>c?.passed===true);
      if(passed)this.attempts=0;
      return {...base,status:passed?'ready':'degraded',protocol:'responding',capability:passed?'health_checks_passed':'health_checks_failed',checks:Array.isArray(checks)?checks.map(c=>({code:c.code,passed:c.passed===true})):[],next_action:passed?'本地接口及健康项通过；具体视频任务完成情况需查看该任务。':'接口已响应，但环境健康检查存在失败；交技术服务定位。'};
    }catch{return {...base,status:'unknown',protocol:'unconfirmed',next_action:'未确认健康响应；保留现场，先核对活动任务和进程，不能据此判定卡死。'};}
    finally{if(timer)clearTimeout(timer);}
  }
  repair(actor:ExecutorIdentity):Promise<unknown>{
    this.policy.require(actor,'runtime.restart');if(this.recovering)return this.recovering;
    const work=(async()=>{
      const before=await this.inspect(actor);
      if(before.status==='ready'||before.status==='busy')return {...before,recovery:'not_needed'};
      const snapshot=this.backend.snapshot();
      if(snapshot.running||snapshot.pending.length)throw new ExpertError('RUNTIME_ACTIVE_OR_UNCONFIRMED',409);
      if(!snapshot.data_root)throw new ExpertError('RUNTIME_ROOT_NOT_CONFIGURED',409);
      if(this.attempts>=2)throw new ExpertError('RUNTIME_REPAIR_LIMIT',409);
      this.attempts++;this.event('start_owned_backend','started');
      try{
        await this.backend.start(snapshot.data_root);const after=await this.inspect(actor);
        this.event('verify_owned_backend',after.status);
        return {...after,recovery:after.status==='ready'?'recovered':'failed',retained:['existing_tasks','original_request'],automatic_task_resubmission:false};
      }catch{this.event('start_owned_backend','failed');throw new ExpertError('RUNTIME_RECOVERY_FAILED',502);}
    })();this.recovering=work;void work.finally(()=>{this.recovering=null;}).catch(()=>undefined);return work;
  }
}
