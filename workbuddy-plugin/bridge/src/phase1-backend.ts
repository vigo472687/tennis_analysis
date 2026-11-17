import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {StringDecoder} from 'node:string_decoder';
import {CONTRACT_VERSION} from './contracts.js';

export class Phase1Error extends Error {
  constructor(readonly code:string,readonly status=422){super(code);}
}
export const REAL_VIDEO_TIMEOUT_MS=30*60*1000;
export const LONG_RUNNING_METHODS=new Set(['evidence.render']);
export interface BackendReply {
  contract_version:string;request_id:string;ok:boolean;simulation:boolean;
  result:Record<string,unknown>|null;error:({code:string}&Record<string,unknown>)|null;
}
export interface Exchange {
  sequence:number;at:string;session_id:string;method:string;request_line:string;
  response:BackendReply;elapsed_ms:number;
}
interface Active {
  child:ChildProcessWithoutNullStreams;line:string;method:string;started:number;
  id:string|null;transport:boolean;
  resolve:(value:Exchange)=>void;reject:(error:Error)=>void;timer:NodeJS.Timeout;
}

export function createFixtureParams(operationId:string,fixture='iphone-flat'){
  return {
    operation_id:operationId,fixture_id:fixture,media_ref:'fixtures/media/flat-forehand.mp4',
    user:{age_band:'adult',handedness:'right',target_stroke:'forehand',goal:'稳定击球点',exercise_background:'两年休闲球龄',safety:'clear'},
    consent:{consent_id:'consent-phase1',task_version:1,purposes:['analysis'],text_version:'consent-text-1',confirmed:true,guardian_simulated:true,recipient_digest:null,materials_digest:null},
    primary_route:'technical_diagnosis',auxiliary_routes:['tactics'],subject_id:null,mirror_confirmed:false
  };
}
export function envelope(method:string,params:Record<string,unknown>,simulation=method!=='runtime.status',operationId:string|null=null){
  return {contract_version:CONTRACT_VERSION,request_id:'req-'+randomUUID(),operation_id:operationId,method,params,simulation};
}
export function findUv():string {
  for(const name of [...String(process.env.PATH||'').split(path.delimiter).map(p=>path.join(p,process.platform==='win32'?'uv.exe':'uv')),path.join(os.homedir(),'.local/bin/uv'),'/opt/homebrew/bin/uv']){
    try{fs.accessSync(name,fs.constants.X_OK);return fs.realpathSync(name);}catch{}
  }
  throw new Phase1Error('UV_UNAVAILABLE');
}

/** Correlate out-of-order worker/control replies; isolate unattributed transport errors. */
export class Phase1Backend {
  private child:ChildProcessWithoutNullStreams|null=null;
  private readonly active=new Set<Active>();
  private stdout='';private stderr='';
  private stdoutDecoder=new StringDecoder('utf8');private stderrDecoder=new StringDecoder('utf8');
  private lifecycle:'stopped'|'starting'|'running'|'stopping'|'error'='stopped';
  private dataRoot:string|null=null;private sessionId:string|null=null;private exitCode:number|null=null;
  private health:Record<string,unknown>|null=null;private lastError:string|null=null;
  private readonly diagnostics:{at:string;message:string}[]=[];
  private readonly history:Exchange[]=[];
  private readonly transitions:{at:string;task_id:string;state:unknown;stage:unknown;checkpoints:unknown;sequence:number}[]=[];
  private sequence=0;private stdoutLines=0;

  constructor(readonly repoRoot:string,private readonly timeoutMs=15000){}
  get running():boolean{return Boolean(this.child&&this.child.exitCode===null&&!this.child.killed);}
  snapshot(){return {
    lifecycle:this.lifecycle,running:this.running,pid:this.running?this.child?.pid:null,
    data_root:this.dataRoot,session_id:this.sessionId,exit_code:this.exitCode,error:this.lastError,
    contract_version:CONTRACT_VERSION,analysis_mode:'fixture+real_video',stdout_lines:this.stdoutLines,
    pending:[...this.active].map(x=>({request_id:x.id,method:x.method,request_line:x.line,elapsed_ms:Date.now()-x.started})),
    health:this.health,diagnostics:[...this.diagnostics],history:[...this.history],transitions:[...this.transitions]
  };}
  private diagnostic(message:string){
    let safe=message.replace(/[\x00-\x08\x0b-\x1f\x7f]/g,'');
    for(const [original,replacement] of [[this.repoRoot,'[project]'],[this.dataRoot,'[data-root]'],[os.homedir(),'[home]']] as const){if(original)safe=safe.split(original).join(replacement);}
    safe=safe.replace(/(?:Bearer\s+)[^\s]+/gi,'Bearer [redacted]').replace(/((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s]+/gi,'$1[redacted]');
    this.diagnostics.push({at:new Date().toISOString(),message:safe.slice(0,1600)});
    if(this.diagnostics.length>100)this.diagnostics.shift();
  }
  async start(root:string|null){
    if(this.running)throw new Phase1Error('BACKEND_ALREADY_RUNNING',409);
    const uv=findUv();this.dataRoot=root?.trim()?path.resolve(this.repoRoot,root.trim()):null;
    this.health=null;this.exitCode=null;this.lastError=null;this.stdoutLines=0;this.sessionId=randomUUID();
    this.stdout='';this.stderr='';this.stdoutDecoder=new StringDecoder('utf8');this.stderrDecoder=new StringDecoder('utf8');
    this.lifecycle='starting';
    const env:NodeJS.ProcessEnv={PYTHONNOUSERSITE:'1'};
    for(const key of ['PATH','HOME','TMPDIR','TEMP','LANG','LC_ALL','SYSTEMROOT'])if(process.env[key])env[key]=process.env[key];
    if(this.dataRoot)env.TENNIS_DATA_ROOT=this.dataRoot;
    const child=spawn(uv,['run','--frozen','--offline','--project',path.join(this.repoRoot,'local-backend'),'tennis-backend','--stdio'],{cwd:this.repoRoot,env,shell:false,stdio:['pipe','pipe','pipe']});
    this.child=child;
    const exit=new Promise<void>(resolve=>{
      child.once('error',()=>{if(this.child===child){this.lifecycle='error';this.lastError='BACKEND_START_FAILED';this.failActive('BACKEND_START_FAILED');this.child=null;}resolve();});
      child.once('exit',code=>{
        if(this.child===child){
          this.exitCode=code;if(this.stderr.trim())this.diagnostic(this.stderr.trim());this.stderr='';
          const stopped=this.lifecycle==='stopping';this.lifecycle=stopped?'stopped':'error';
          this.lastError=stopped?null:'BACKEND_EXIT';this.failActive('BACKEND_EXIT');this.child=null;
        }
        resolve();
      });
    });
    child.stdout.on('data',(chunk:Buffer)=>{if(this.child===child)this.receive(child,chunk);});
    child.stderr.on('data',(chunk:Buffer)=>{
      if(this.child!==child)return;this.stderr+=this.stderrDecoder.write(chunk);
      let end=this.stderr.indexOf('\n');while(end>=0){this.diagnostic(this.stderr.slice(0,end));this.stderr=this.stderr.slice(end+1);end=this.stderr.indexOf('\n');}
      if(this.stderr.length>8192){this.diagnostic(this.stderr);this.stderr='';}
    });
    if(!this.dataRoot){
      let timer:NodeJS.Timeout|undefined;
      await Promise.race([exit,new Promise<void>(resolve=>{timer=setTimeout(resolve,this.timeoutMs);})]);if(timer)clearTimeout(timer);
      if(this.running){this.lastError='STARTUP_REFUSAL_TIMEOUT';await this.stop();this.lifecycle='error';this.lastError='STARTUP_REFUSAL_TIMEOUT';}
      return this.snapshot();
    }
    try{
      const reply=await this.request(JSON.stringify(envelope('runtime.status',{runtime_requirement:'not_required'},false)));
      if(this.child===child&&this.running){this.lifecycle='running';if(!reply.response.ok)this.lastError=reply.response.error?.code||'BACKEND_HEALTH_FAILED';}
    }catch(error){if(this.child===child){this.lifecycle='error';this.lastError=error instanceof Phase1Error?error.code:'BACKEND_START_FAILED';await this.stop();this.lifecycle='error';}}
    return this.snapshot();
  }
  request(line:string):Promise<Exchange>{
    if(typeof line!=='string'||!line.trim()||/[\r\n]/.test(line))return Promise.reject(new Phase1Error('ONE_LINE_REQUIRED',400));
    if(Buffer.byteLength(line)>1152*1024)return Promise.reject(new Phase1Error('REQUEST_TOO_LARGE',413));
    return this.write(line);
  }
  private write(line:string):Promise<Exchange>{
    if(!this.running||!this.child)return Promise.reject(new Phase1Error('BACKEND_NOT_RUNNING',409));
    const child=this.child;let method='原始协议行',id:string|null=null,real=false;
    try{const p=JSON.parse(line);if(typeof p?.method==='string')method=p.method;if(typeof p?.request_id==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(p.request_id))id=p.request_id;real=method==='analysis.create'&&p.params?.analysis_mode==='real_video';}catch{}
    real=real||LONG_RUNNING_METHODS.has(method);
    const transport=id===null||Buffer.byteLength(line)>1048576;
    if([...this.active].some(x=>id!==null&&x.id===id))return Promise.reject(new Phase1Error('REQUEST_IN_FLIGHT',409));
    if(transport&&[...this.active].some(x=>x.transport))return Promise.reject(new Phase1Error('RAW_REQUEST_IN_FLIGHT',409));
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{if(this.active.has(pending)){this.failActive('BACKEND_TIMEOUT');child.kill('SIGTERM');}},real?REAL_VIDEO_TIMEOUT_MS:this.timeoutMs);
      const pending:Active={child,line,method,id,transport,started:Date.now(),resolve,reject,timer};this.active.add(pending);
      child.stdin.write(line+'\n',error=>{if(error&&this.active.has(pending))this.failActive('BACKEND_WRITE_FAILED');});
    });
  }
  private receive(child:ChildProcessWithoutNullStreams,chunk:Buffer){
    this.stdout+=this.stdoutDecoder.write(chunk);
    if(Buffer.byteLength(this.stdout)>2*1024*1024){this.failActive('BACKEND_PROTOCOL_INVALID');child.kill('SIGTERM');return;}
    let end=this.stdout.indexOf('\n');
    while(end>=0){
      const line=this.stdout.slice(0,end);this.stdout=this.stdout.slice(end+1);this.stdoutLines++;
      try{
        const reply=JSON.parse(line) as BackendReply;
        let pending=[...this.active].find(x=>x.id===reply.request_id);
        if(!pending&&/^transport-\d+$/.test(reply.request_id))pending=[...this.active].find(x=>x.transport);
        if(!pending&&this.active.size===1&&/^transport-\d+$/.test(reply.request_id))pending=[...this.active][0];
        if(!pending||pending.child!==child||typeof reply.ok!=='boolean')throw new Error('invalid response');
        clearTimeout(pending.timer);this.active.delete(pending);
        const item:Exchange={sequence:++this.sequence,at:new Date().toISOString(),session_id:this.sessionId!,method:pending.method,request_line:pending.line,response:reply,elapsed_ms:Date.now()-pending.started};
        this.history.push(item);if(this.history.length>40)this.history.shift();
        if(pending.method==='runtime.status'&&reply.ok)this.health=reply.result;
        const result=reply.result;if(reply.ok&&result&&typeof result.task_id==='string'){
          this.transitions.push({at:item.at,task_id:result.task_id,state:result.state,stage:result.stage,checkpoints:result.checkpoints,sequence:item.sequence});if(this.transitions.length>60)this.transitions.shift();
        }
        pending.resolve(item);
      }catch{this.failActive('BACKEND_PROTOCOL_INVALID');child.kill('SIGTERM');return;}
      end=this.stdout.indexOf('\n');
    }
  }
  private failActive(code:string){for(const pending of this.active){clearTimeout(pending.timer);pending.reject(new Phase1Error(code,502));}this.active.clear();}
  async stop(){
    const child=this.child;if(!child){this.lifecycle='stopped';return this.snapshot();}
    this.lifecycle='stopping';this.failActive('BACKEND_STOPPED');
    await new Promise<void>(resolve=>{
      if(child.exitCode!==null){resolve();return;}
      const timer=setTimeout(()=>child.kill('SIGKILL'),1200);
      child.once('exit',()=>{clearTimeout(timer);resolve();});child.kill('SIGTERM');
    });
    if(this.child===child)this.child=null;this.lifecycle='stopped';return this.snapshot();
  }
}
