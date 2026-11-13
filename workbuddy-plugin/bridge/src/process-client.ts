import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {StringDecoder} from 'node:string_decoder';
import fs from 'node:fs';
import path from 'node:path';
import {ContractRegistry,ContractError,type RequestEnvelope,type ResponseEnvelope} from './contracts.js';
import {SafeLogger} from './logging.js';

interface ProcessOptions {command:string;args:string[];cwd:string;timeoutMs?:number;maxLineBytes?:number;env?:NodeJS.ProcessEnv;}
interface Pending {method:string;resolve:(r:ResponseEnvelope)=>void;reject:(e:Error)=>void;timer:NodeJS.Timeout;}
function executableAllowed(command:string):boolean {
  if([process.execPath,'node','uv','python3','python3.11'].includes(command))return true;
  if(!path.isAbsolute(command)||!['uv','python3','python3.11'].includes(path.basename(command)))return false;
  try{const stat=fs.statSync(fs.realpathSync(command));return stat.isFile()&&(stat.mode&0o111)!==0;}catch{return false;}
}

export class ProcessClient {
  private child:ChildProcessWithoutNullStreams|null=null;
  private starting:Promise<void>|null=null;
  private readonly pending=new Map<string,Pending>();
  private readonly seen=new Set<string>();
  private readonly expired=new Set<string>();
  private buffer='';
  private decoder=new StringDecoder('utf8');
  constructor(private readonly registry:ContractRegistry,private readonly options:ProcessOptions,private readonly logger=new SafeLogger()){
    if(!executableAllowed(options.command)||options.args.some(a=>a.includes('\0')))throw new ContractError('EXECUTABLE_FORBIDDEN');
  }
  get running():boolean{return !!this.child&&this.child.exitCode===null&&!this.child.killed;}
  async start():Promise<void>{
    if(this.starting)return this.starting;if(this.running)return;
    this.starting=this.spawn();
    try{await this.starting;}finally{this.starting=null;}
  }
  private async spawn():Promise<void>{
    const env:NodeJS.ProcessEnv={};
    for(const key of ['PATH','HOME','TMPDIR','TEMP','LANG','LC_ALL','SYSTEMROOT'])if(process.env[key])env[key]=process.env[key];
    for(const [key,value]of Object.entries(this.options.env||{}))if(/^TENNIS_|^UV_(CACHE_DIR|PYTHON_INSTALL_DIR|OFFLINE)$/.test(key))env[key]=value;
    const child=spawn(this.options.command,this.options.args,{cwd:this.options.cwd,env,shell:false,stdio:['pipe','pipe','pipe']});
    this.child=child;this.buffer='';this.decoder=new StringDecoder('utf8');
    child.stdout.on('data',(chunk:Buffer)=>this.receive(chunk));
    child.stderr.on('data',()=>this.logger.log({stage:'runtime',code:'BACKEND_DIAGNOSTIC',outcome:'pending',simulation:false}));
    child.on('error',()=>{this.failAll('BACKEND_START_FAILED');if(this.child===child)this.child=null;});
    child.on('exit',()=>{this.failAll('BACKEND_EXIT');if(this.child===child)this.child=null;});
    try{await once(child,'spawn');}catch{throw new ContractError('BACKEND_START_FAILED');}
  }
  call(request:RequestEnvelope):Promise<ResponseEnvelope>{
    this.registry.request(request);
    if(!this.running||!this.child)return Promise.reject(new ContractError('RUNTIME_NOT_READY'));
    if(this.seen.has(request.request_id))return Promise.reject(new ContractError('REQUEST_ID_REUSED'));
    if(this.seen.size>=10000)return Promise.reject(new ContractError('RUNTIME_RESTART_REQUIRED'));
    this.seen.add(request.request_id);
    const line=JSON.stringify(request)+'\n';
    if(Buffer.byteLength(line)>(this.options.maxLineBytes??1048576))return Promise.reject(new ContractError('REQUEST_TOO_LARGE'));
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(request.request_id);this.expired.add(request.request_id);if(this.expired.size>256){const first=this.expired.values().next().value;if(first)this.expired.delete(first);}reject(new ContractError('BACKEND_TIMEOUT'));},this.options.timeoutMs??20000);
      this.pending.set(request.request_id,{method:request.method,resolve,reject,timer});
      this.child!.stdin.write(line,error=>{if(error){const pending=this.pending.get(request.request_id);if(pending){clearTimeout(pending.timer);this.pending.delete(request.request_id);reject(new ContractError('BACKEND_WRITE_FAILED'));}}});
    });
  }
  private receive(chunk:Buffer):void{
    this.buffer+=this.decoder.write(chunk);
    if(Buffer.byteLength(this.buffer)>(this.options.maxLineBytes??1048576)){this.protocolFailure();return;}
    let end=this.buffer.indexOf('\n');
    while(end>=0){
      const line=this.buffer.slice(0,end).trim();this.buffer=this.buffer.slice(end+1);
      if(!line){this.protocolFailure();return;}
      try{
        const value=JSON.parse(line) as {request_id?:string};
        if(value.request_id&&this.expired.has(value.request_id)){end=this.buffer.indexOf('\n');continue;}
        const pending=value.request_id?this.pending.get(value.request_id):undefined;
        if(!pending)throw new ContractError('BACKEND_UNSOLICITED_RESPONSE');
        const response=this.registry.response(pending.method,value);
        clearTimeout(pending.timer);this.pending.delete(response.request_id);pending.resolve(response);
      }catch{this.protocolFailure();return;}
      end=this.buffer.indexOf('\n');
    }
  }
  private protocolFailure():void{this.failAll('BACKEND_PROTOCOL_INVALID');this.child?.kill('SIGTERM');}
  private failAll(code:string):void{for(const pending of this.pending.values()){clearTimeout(pending.timer);pending.reject(new ContractError(code));}this.pending.clear();}
  async close():Promise<void>{
    const child=this.child;this.failAll('RUNTIME_STOPPED');
    if(!child||child.exitCode!==null)return;
    const exited=once(child,'exit').then(()=>undefined).catch(()=>undefined);
    child.kill('SIGTERM');const force=setTimeout(()=>child.kill('SIGKILL'),1000);
    await exited;clearTimeout(force);if(this.child===child)this.child=null;
  }
}
