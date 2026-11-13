import fs from 'node:fs';
import http,{type IncomingMessage,type ServerResponse} from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import type {AddressInfo} from 'node:net';
import {ContractRegistry,ContractError,type JsonObject,type ResponseEnvelope} from './contracts.js';
import {fixtureCatalog,storyCatalog} from './debug-console-catalog.js';
import {appScript,page,styles} from './debug-console-ui.js';
import {FixtureBackend} from './fixture-backend.js';
import {PathBoundary} from './paths.js';
import {ProcessClient} from './process-client.js';
import {RuntimeStore,type RuntimeSnapshot} from './runtime-state.js';
import {ToolDispatcher} from './tools.js';
import {VideoExperience} from './video-experience.js';

type BackendMode='python'|'fixture';
type Lifecycle='stopped'|'starting'|'ready'|'stopping'|'error';
type Outcome='success'|'failure'|'pending';
type FixtureFault='none'|'timeout'|'crash'|'invalid_response'|'delete_partial';

const CONSOLE_VERSION='0.2.0';
const CONTRACT_VERSION='1.1.0';
const BACKEND_VERSION='0.1.0';
const DEFAULT_HOST='127.0.0.1';
const DEFAULT_PORT=4178;
const MAX_BODY_BYTES=256*1024;
const HISTORY_LIMIT=200;
const RESOURCE_MIME=new Set(['image/png','video/mp4','application/json']);

interface SessionEvent {timestamp:string;action:string;outcome:Outcome;code:string;mode:BackendMode;}
interface ErrorSummary {code:string;message:string;pointers?:readonly string[];}
interface ConsoleOptions {repoRoot:string;dataRoot:string;}
interface ToolHistory {
  timestamp:string;tool:string;method:string;mode:BackendMode;outcome:'success'|'failure';duration_ms:number;
  code:string|null;request_id:string|null;task_id:string|null;operation_id:string|null;simulation:boolean;
}
interface TaskRecord {
  task_id:string;version:number;state:string;stage:string;mode:BackendMode;fixture_id:string|null;simulation:boolean;updated_at:string;
}
interface ResourceRecord {
  resource_id:string;path:string;mime:string;sha256:string;size_bytes:number;kind:string;simulation:boolean;
  task_id:string;version:number;mode:BackendMode;
}

function stableError(error:unknown):ErrorSummary {
  const code=error instanceof ContractError?error.code:error instanceof Error&&/^[A-Z][A-Z0-9_]+$/.test(error.message)?error.message:'DEBUG_CONSOLE_OPERATION_FAILED';
  const messages:Record<string,string>={
    BACKEND_START_FAILED:'The Python backend could not start.',
    BACKEND_EXIT:'The Python backend exited before replying.',
    BACKEND_TIMEOUT:'The Python backend did not reply before the timeout.',
    BACKEND_PROTOCOL_INVALID:'The Python backend returned invalid NDJSON.',
    BACKEND_HEALTH_FAILED:'One or more runtime health checks failed.',
    BACKEND_MODE_INVALID:'The backend mode is not supported.',
    CONTRACT_INVALID:'The request does not match the frozen Contract v1 schema.',
    FIXTURE_FAULT_INVALID:'The selected fixture fault is not supported.',
    REQUEST_INVALID:'The request body must be a JSON object.',
    REQUEST_TOO_LARGE:'The request body exceeds the local console limit.',
    RESOURCE_NOT_REGISTERED:'The resource is not registered in this console session.',
    RESOURCE_UNAVAILABLE:'The registered resource is no longer available.',
    RESOURCE_MIME_FORBIDDEN:'The resource MIME type is not previewable.',
    RUNTIME_NOT_PREPARED:'The local runtime has not been prepared.',
    RUNTIME_TRANSITION_INVALID:'The runtime state cannot perform that action.',
    TOOL_UNKNOWN:'The requested Contract v1 tool does not exist.',
    UV_UNAVAILABLE:'The locked Python runner is unavailable.',
    DEBUG_CONSOLE_OPERATION_FAILED:'The debug console could not complete the operation.'
  };
  const summary:ErrorSummary={code,message:messages[code]||'The operation failed. Review the structured status and retry.'};
  if(error instanceof ContractError&&error.pointers.length)summary.pointers=error.pointers;
  return summary;
}

function freeSpaceAvailable(root:string):boolean {
  let current=root;
  while(!fs.existsSync(current)){
    const parent=path.dirname(current);if(parent===current)return false;current=parent;
  }
  try{const stat=fs.statfsSync(current);return stat.bavail*stat.bsize>=64*1024*1024;}catch{return false;}
}

function uvExecutable():string {
  const candidates=[
    ...(process.env.PATH||'').split(path.delimiter).filter(Boolean).map(directory=>path.join(directory,'uv')),
    path.join(os.homedir(),'.local/bin/uv'),
    '/opt/homebrew/bin/uv',
    '/usr/local/bin/uv'
  ];
  for(const candidate of candidates){try{fs.accessSync(candidate,fs.constants.X_OK);return fs.realpathSync(candidate);}catch{}}
  throw new ContractError('UV_UNAVAILABLE');
}

function identifier(value:unknown):string|null {
  return typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)?value:null;
}

function objectOf(value:unknown):JsonObject|null {
  return value!==null&&typeof value==='object'&&!Array.isArray(value)?value as JsonObject:null;
}

function resourcesIn(value:unknown,result:JsonObject[]=[]):JsonObject[] {
  if(Array.isArray(value)){for(const item of value)resourcesIn(item,result);return result;}
  const object=objectOf(value);if(!object)return result;
  if(['resource_id','path','mime','sha256','size_bytes','kind','simulation'].every(key=>key in object))result.push(object);
  for(const item of Object.values(object))resourcesIn(item,result);
  return result;
}

class DebugRuntime {
  readonly registry:ContractRegistry;
  readonly store:RuntimeStore;
  readonly tools:ToolDispatcher;
  readonly process:ProcessClient;
  readonly modeRoot:string;
  private readonly fixture:FixtureBackend;
  private fixtureRunning=false;

  constructor(private readonly options:ConsoleOptions,readonly mode:BackendMode,fault:FixtureFault='none'){
    const contractRoot=path.join(options.repoRoot,'contracts/v1');
    const fixtureRoot=path.join(options.repoRoot,'fixtures');
    const assetRoot=path.join(options.repoRoot,'workbuddy-plugin/fixtures/assets');
    const projectRoot=path.join(options.repoRoot,'local-backend');
    this.modeRoot=path.join(options.dataRoot,mode);
    this.registry=new ContractRegistry(contractRoot);
    this.store=new RuntimeStore(this.registry,this.modeRoot,mode);
    this.fixture=new FixtureBackend(this.registry,{
      fixtureRoot,dataRoot:path.join(this.modeRoot,'public'),assetRoot,delayMs:mode==='fixture'?700:undefined,
      fault:fault==='none'?undefined:fault
    });
    this.process=new ProcessClient(this.registry,{
      command:uvExecutable(),
      args:['run','--frozen','--offline','--project',projectRoot,'tennis-backend','--stdio'],
      cwd:options.repoRoot,
      timeoutMs:20000,
      env:{TENNIS_CONTRACT_ROOT:contractRoot,TENNIS_FIXTURE_ROOT:fixtureRoot,TENNIS_DATA_ROOT:this.modeRoot}
    });
    const scope=`${mode}-debug-console-v1`;
    const verify=async(version:string)=>{
      if(version!==BACKEND_VERSION)throw new ContractError('ARTIFACT_VERSION_UNAVAILABLE');
      if(mode==='fixture'){
        const ready=fs.existsSync(path.join(fixtureRoot,'manifest.json'))&&fs.existsSync(path.join(assetRoot,'iphone-flat.mp4'));
        if(!ready)throw new ContractError('FIXTURE_ASSET_MISSING');
        return [{code:'fixture_assets_ready',passed:true},{code:'contract_version_1_0_0',passed:true}];
      }
      const checks=[
        {code:'python_project_present',passed:fs.existsSync(path.join(projectRoot,'pyproject.toml'))},
        {code:'python_lock_present',passed:fs.existsSync(path.join(projectRoot,'uv.lock'))},
        {code:'contract_version_1_0_0',passed:fs.existsSync(path.join(contractRoot,'common.schema.json'))}
      ];
      if(checks.some(check=>!check.passed))throw new ContractError('PYTHON_ARTIFACT_MISSING');
      return checks;
    };
    const health=async()=>{
      if(mode==='fixture')return [{code:'fixture_ready',passed:this.fixtureRunning},{code:'contract_version_1_0_0',passed:true}];
      const reply=await this.process.call({contract_version:CONTRACT_VERSION,request_id:`health-${randomUUID()}`,operation_id:null,method:'runtime.status',params:{runtime_requirement:'required'},simulation:false});
      if(!reply.ok)throw new ContractError(reply.error?.code||'BACKEND_HEALTH_FAILED');
      const result=reply.result as RuntimeSnapshot;
      if(result.state!=='ready')throw new ContractError('BACKEND_HEALTH_FAILED');
      return result.checks;
    };
    const closeBackend=async()=>{this.fixtureRunning=false;this.fixture.close();await this.process.close();};
    this.tools=new ToolDispatcher(this.registry,{
      contractRoot,store:this.store,simulation:true,authorizedScope:scope,backend:mode==='fixture'?this.fixture:this.process,
      prepare:{checkSpace:()=>freeSpaceAvailable(this.modeRoot),verifyArtifact:verify},
      start:{isRunning:()=>this.running,startBackend:async()=>{if(mode==='fixture')this.fixtureRunning=true;else await this.process.start();},verifyBackend:health},
      repair:{authorizedScope:scope,repairArtifact:async version=>({version:version||BACKEND_VERSION,checks:await verify(version||BACKEND_VERSION)})},
      upgrade:{authorizedScope:scope,verifyTarget:verify,restorePrevious:async version=>{await verify(version);return true;}},
      uninstall:{projectDeletionConfirmed:false,stopBackend:closeBackend,removeComponents:async()=>[{kind:'components',removed:true}],deleteProjects:async()=>[]}
    });
  }

  get running():boolean{return this.mode==='fixture'?this.fixtureRunning:this.process.running;}

  async start():Promise<RuntimeSnapshot>{
    fs.mkdirSync(path.join(this.modeRoot,'public'),{recursive:true,mode:0o700});
    fs.mkdirSync(path.join(this.modeRoot,'internal'),{recursive:true,mode:0o700});
    const current=this.store.snapshot();
    if(['not_initialized','repair_required'].includes(current.state)){
      this.requireOk(await this.tools.call('runtime_prepare',{operation_id:`prepare-${randomUUID()}`,consent_confirmed:true,target_version:BACKEND_VERSION,intent:'prepare',scope_digest:`${this.mode}-debug-console-v1`}));
    }
    this.requireOk(await this.tools.call('runtime_start',{operation_id:`start-${randomUUID()}`,runtime_requirement:'required'}));
    return this.store.snapshot();
  }

  async close():Promise<void>{
    this.fixtureRunning=false;this.fixture.close();await this.process.close();
    if(this.store.snapshot().state==='ready')this.store.transition('installed_stopped');
  }

  private requireOk(response:ResponseEnvelope):void {if(!response.ok)throw new ContractError(response.error?.code||'DEBUG_CONSOLE_OPERATION_FAILED');}
}

class SessionController {
  private runtime:DebugRuntime|null=null;
  private mode:BackendMode='python';
  private lifecycle:Lifecycle='stopped';
  private lastError:ErrorSummary|null=null;
  private fault:FixtureFault='none';
  private readonly events:SessionEvent[]=[];
  private readonly history:ToolHistory[]=[];
  private readonly tasks=new Map<string,TaskRecord>();
  private readonly resources=new Map<string,ResourceRecord>();

  constructor(private readonly options:ConsoleOptions){}

  async start(mode:BackendMode=this.mode):Promise<ReturnType<SessionController['snapshot']>>{
    if(this.lifecycle==='ready'&&this.runtime?.mode===mode&&this.runtime.running)return this.snapshot();
    this.lifecycle='starting';this.lastError=null;this.push('session_start','pending','STARTING',mode);
    try{
      if(this.runtime&&this.runtime.mode!==mode)await this.runtime.close();
      if(!this.runtime||this.runtime.mode!==mode)this.runtime=new DebugRuntime(this.options,mode,this.fault);
      this.mode=mode;await this.runtime.start();this.lifecycle='ready';this.push('session_start','success','READY',mode);return this.snapshot();
    }catch(error){
      this.lastError=stableError(error);this.lifecycle='error';this.push('session_start','failure',this.lastError.code,mode);
      if(this.runtime)await this.runtime.close().catch(()=>undefined);
      return this.snapshot();
    }
  }

  async stop():Promise<ReturnType<SessionController['snapshot']>>{
    if(!this.runtime){this.lifecycle='stopped';return this.snapshot();}
    this.lifecycle='stopping';this.push('session_stop','pending','STOPPING',this.mode);
    try{await this.runtime.close();this.lifecycle='stopped';this.lastError=null;this.push('session_stop','success','STOPPED',this.mode);}
    catch(error){this.lastError=stableError(error);this.lifecycle='error';this.push('session_stop','failure',this.lastError.code,this.mode);}
    return this.snapshot();
  }

  async restart(mode:BackendMode=this.mode):Promise<ReturnType<SessionController['snapshot']>>{await this.stop();return this.start(mode);}
  async close():Promise<void>{if(this.runtime)await this.runtime.close().catch(()=>undefined);this.lifecycle='stopped';}

  toolDefinitions(){return this.ensureRuntime().tools.definitions();}

  async callTool(name:string,params:JsonObject):Promise<{response:ResponseEnvelope;history:ToolHistory}>{
    const runtime=this.ensureRuntime();
    const started=Date.now();
    let method=name;
    try{
      const tool=runtime.registry.tool(name);method=tool.method;runtime.registry.input(tool.name,params);
      const response=await runtime.tools.call(tool.name,params);
      const record=this.record(tool.name,method,params,response,Date.now()-started);
      if(response.ok)this.harvest(tool.name,params,response.result,response.simulation);
      this.syncLifecycle(tool.name,response);
      return {response,history:record};
    }catch(error){
      const safe=stableError(error);
      this.recordFailure(name,method,params,safe.code,Date.now()-started);
      throw error;
    }
  }

  historySnapshot(){return {limit:HISTORY_LIMIT,items:[...this.history]};}
  taskSnapshot(){
    return {
      tasks:[...this.tasks.values()].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)),
      resources:[...this.resources.values()].sort((a,b)=>a.path.localeCompare(b.path))
    };
  }

  faultSnapshot(){return {fault:this.fault};}

  async setFault(value:unknown):Promise<{fault:FixtureFault;session:ReturnType<SessionController['snapshot']>}>{
    const allowed:FixtureFault[]=['none','timeout','crash','invalid_response','delete_partial'];
    if(typeof value!=='string'||!allowed.includes(value as FixtureFault))throw new ContractError('FIXTURE_FAULT_INVALID');
    this.fault=value as FixtureFault;
    const restartFixture=this.runtime?.mode==='fixture'&&this.lifecycle==='ready';
    if(this.runtime?.mode==='fixture'){
      await this.runtime.close().catch(()=>undefined);this.runtime=null;this.lifecycle='stopped';
    }
    if(restartFixture)await this.start('fixture');
    return {fault:this.fault,session:this.snapshot()};
  }

  resolveResource(reference:string):{absolute:string;mime:string;size:number}{
    const resource=[...this.resources.values()].find(item=>item.path===reference);
    if(!resource)throw new ContractError('RESOURCE_NOT_REGISTERED');
    if(!RESOURCE_MIME.has(resource.mime))throw new ContractError('RESOURCE_MIME_FORBIDDEN');
    const publicRoot=path.join(this.options.dataRoot,resource.mode,'public');
    const absolute=new PathBoundary(publicRoot,publicRoot).publicTask(resource.path,resource.task_id,resource.version);
    if(!fs.existsSync(absolute)||!fs.statSync(absolute).isFile())throw new ContractError('RESOURCE_UNAVAILABLE');
    return {absolute,mime:resource.mime,size:fs.statSync(absolute).size};
  }

  snapshot(){
    const runtime=this.runtime?.store.snapshot()||{state:'not_initialized',version:null,backend:this.mode,checks:[],retained:[],simulation:this.mode==='fixture'};
    return {
      console_version:CONSOLE_VERSION,
      contract_version:CONTRACT_VERSION,
      mode:this.mode,
      lifecycle:this.lifecycle,
      data_root:this.runtime?.modeRoot||path.join(this.options.dataRoot,this.mode),
      backend:{running:this.runtime?.running||false,state:runtime.state,version:runtime.version,simulation:runtime.simulation,checks:runtime.checks},
      last_error:this.lastError,
      events:[...this.events]
    };
  }

  capabilities(){return {
    measured:[
      {id:'runtime-health',label:'Runtime health',detail:'Python, Contract, FFmpeg, ffprobe and authorized roots.'},
      {id:'media-probe',label:'Media probe',detail:'ffprobe metadata and compatibility checks.'},
      {id:'media-normalize',label:'Media normalization',detail:'FFmpeg normalization and time mapping.'},
      {id:'resource-integrity',label:'Resource integrity',detail:'Generated resource hashes and bounded local paths.'}
    ],
    simulated:[
      {id:'fixture-analysis',label:'Fixture analysis',detail:'Fixed-sample strokes, timing and relative metrics.'},
      {id:'target-pose',label:'Target pose',detail:'Teaching illustration only; never current-video evidence.'}
    ],
    unavailable:[
      {id:'real-pose',label:'Real dynamic pose',detail:'No validated real-video pose adapter is connected.'},
      {id:'ball-racket',label:'Ball and racket tracking',detail:'No ball, racket-head or impact detector is connected.'},
      {id:'real-upload',label:'Arbitrary video upload',detail:'Contract v1 still requires a fixture_id.'}
    ]
  };}

  private ensureRuntime():DebugRuntime {
    if(!this.runtime)this.runtime=new DebugRuntime(this.options,this.mode,this.fault);
    return this.runtime;
  }

  private record(name:string,method:string,params:JsonObject,response:ResponseEnvelope,duration:number):ToolHistory {
    const item:ToolHistory={
      timestamp:new Date().toISOString(),tool:name,method,mode:this.mode,outcome:response.ok?'success':'failure',duration_ms:duration,
      code:response.error?.code||null,request_id:identifier(response.request_id),task_id:identifier(params.task_id)||identifier(objectOf(response.result)?.task_id),
      operation_id:identifier(params.operation_id),simulation:response.simulation
    };
    this.history.unshift(item);if(this.history.length>HISTORY_LIMIT)this.history.length=HISTORY_LIMIT;return item;
  }

  private recordFailure(name:string,method:string,params:JsonObject,code:string,duration:number):void {
    const item:ToolHistory={timestamp:new Date().toISOString(),tool:name,method,mode:this.mode,outcome:'failure',duration_ms:duration,code,request_id:null,task_id:identifier(params.task_id),operation_id:identifier(params.operation_id),simulation:this.mode==='fixture'};
    this.history.unshift(item);if(this.history.length>HISTORY_LIMIT)this.history.length=HISTORY_LIMIT;
  }

  private harvest(name:string,params:JsonObject,value:unknown,simulation:boolean):void {
    const result=objectOf(value);
    const resultTask=identifier(result?.task_id);
    const paramTask=identifier(params.task_id);
    const taskId=resultTask||paramTask;
    const version=typeof result?.version==='number'?result.version:typeof params.version==='number'?params.version:null;
    if(taskId&&version&&Number.isInteger(version)){
      const previous=this.tasks.get(taskId);
      this.tasks.set(taskId,{
        task_id:taskId,version,state:typeof result?.state==='string'?result.state:previous?.state||'unknown',
        stage:typeof result?.stage==='string'?result.stage:previous?.stage||'none',mode:this.mode,
        fixture_id:name==='create_video_analysis'&&typeof params.fixture_id==='string'?params.fixture_id:previous?.fixture_id||null,
        simulation,updated_at:new Date().toISOString()
      });
    }
    if(taskId&&version){
      for(const candidate of resourcesIn(value)){
        const resourceId=identifier(candidate.resource_id);const reference=typeof candidate.path==='string'?candidate.path:null;
        if(!resourceId||!reference||typeof candidate.mime!=='string'||typeof candidate.kind!=='string'||typeof candidate.sha256!=='string'||typeof candidate.size_bytes!=='number')continue;
        this.resources.set(resourceId,{resource_id:resourceId,path:reference,mime:candidate.mime,sha256:candidate.sha256,size_bytes:candidate.size_bytes,kind:candidate.kind,simulation:Boolean(candidate.simulation),task_id:taskId,version,mode:this.mode});
      }
    }
    if(name==='delete_analysis_data'&&result){
      const scope=params.scope;
      const deleted=new Set((Array.isArray(result.items)?result.items:[]).flatMap(item=>{const object=objectOf(item);return object?.status==='deleted'&&identifier(object.resource_id)?[String(object.resource_id)]:[];}));
      if(scope==='all_demo'&&result.complete===true)for(const task of this.tasks.values())if(task.mode===this.mode)deleted.add(task.task_id);
      for(const id of deleted){const task=this.tasks.get(id);if(task)this.tasks.set(id,{...task,state:'deleted',stage:'none',updated_at:new Date().toISOString()});for(const [resourceId,resource] of this.resources)if(resource.task_id===id)this.resources.delete(resourceId);}
    }
  }

  private syncLifecycle(name:string,response:ResponseEnvelope):void {
    if(!response.ok)return;
    if(name==='runtime_start'){this.lifecycle='ready';this.lastError=null;}
    if(name==='runtime_uninstall'){this.lifecycle='stopped';this.lastError=null;}
    if(['runtime_prepare','runtime_repair'].includes(name)&&!this.runtime?.running)this.lifecycle='stopped';
  }

  private push(action:string,outcome:Outcome,code:string,mode:BackendMode):void {
    this.events.unshift({timestamp:new Date().toISOString(),action,outcome,code,mode});if(this.events.length>20)this.events.length=20;
  }
}

function isLoopbackAuthority(value:string|undefined):boolean {
  if(!value)return false;
  try{const hostname=new URL(`http://${value}`).hostname;return hostname==='127.0.0.1'||hostname==='localhost'||hostname==='[::1]';}catch{return false;}
}
function originAllowed(value:string|undefined):boolean {if(!value)return true;try{return ['127.0.0.1','localhost','[::1]'].includes(new URL(value).hostname);}catch{return false;}}
function headers(type:string):Record<string,string>{return {'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cross-Origin-Resource-Policy':'same-origin','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"};}
function send(res:ServerResponse,status:number,value:string|Buffer,type:string,extra:Record<string,string|number>={}):void {res.writeHead(status,{...headers(type),...extra});res.end(value);}
function json(res:ServerResponse,status:number,value:unknown):void {send(res,status,JSON.stringify(value),'application/json; charset=utf-8');}
async function body(req:IncomingMessage):Promise<JsonObject>{
  const chunks:Buffer[]=[];let size=0;
  for await(const chunk of req){const value=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);size+=value.length;if(size>MAX_BODY_BYTES)throw new ContractError('REQUEST_TOO_LARGE');chunks.push(value);}
  if(!chunks.length)return {};
  const parsed=JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown;
  if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new ContractError('REQUEST_INVALID');return parsed as JsonObject;
}
function modeOf(value:unknown,fallback:BackendMode):BackendMode {if(value===undefined)return fallback;if(value==='python'||value==='fixture')return value;throw new ContractError('BACKEND_MODE_INVALID');}

export interface DebugConsoleOptions {repoRoot:string;dataRoot?:string;host?:string;port?:number;}
export interface DebugConsoleHandle {url:string;controller:SessionController;server:http.Server;close:()=>Promise<void>;}

export async function startDebugConsole(options:DebugConsoleOptions):Promise<DebugConsoleHandle>{
  const host=options.host||DEFAULT_HOST;if(host!==DEFAULT_HOST)throw new ContractError('LOOPBACK_ONLY');
  const repoRoot=path.resolve(options.repoRoot);
  const dataRoot=path.resolve(options.dataRoot||path.join(repoRoot,'tmp/debug-console-data'));
  const controller=new SessionController({repoRoot,dataRoot});
  const experience=new VideoExperience(repoRoot,path.join(dataRoot,'experience'));
  const catalog=storyCatalog(repoRoot);
  const fixtures=fixtureCatalog(repoRoot);
  const server=http.createServer(async(req,res)=>{
    if(!isLoopbackAuthority(req.headers.host)||!originAllowed(req.headers.origin)){json(res,403,{error:{code:'LOCAL_REQUEST_REQUIRED',message:'Only local console requests are accepted.'}});return;}
    const url=new URL(req.url||'/',`http://${req.headers.host}`);
    try{
      if(await experience.route(req,res,url))return;
      if(req.method==='GET'&&url.pathname==='/debug'){send(res,200,page,'text/html; charset=utf-8');return;}
      if(req.method==='GET'&&url.pathname==='/favicon.ico'){res.writeHead(204,headers('image/x-icon'));res.end();return;}
      if(req.method==='GET'&&url.pathname==='/styles.css'){send(res,200,styles,'text/css; charset=utf-8');return;}
      if(req.method==='GET'&&url.pathname==='/app.js'){send(res,200,appScript,'text/javascript; charset=utf-8');return;}
      if(req.method==='GET'&&url.pathname==='/api/session'){json(res,200,controller.snapshot());return;}
      if(req.method==='GET'&&url.pathname==='/api/capabilities'){json(res,200,controller.capabilities());return;}
      if(req.method==='GET'&&url.pathname==='/api/stories'){json(res,200,catalog);return;}
      if(req.method==='GET'&&url.pathname==='/api/fixtures'){json(res,200,{items:fixtures});return;}
      if(req.method==='GET'&&url.pathname==='/api/tools'){json(res,200,{items:controller.toolDefinitions()});return;}
      if(req.method==='GET'&&url.pathname==='/api/history'){json(res,200,controller.historySnapshot());return;}
      if(req.method==='GET'&&url.pathname==='/api/tasks'){json(res,200,controller.taskSnapshot());return;}
      if(req.method==='GET'&&url.pathname==='/api/session/fault'){json(res,200,controller.faultSnapshot());return;}
      if(req.method==='GET'&&url.pathname==='/api/resource'){
        const resource=controller.resolveResource(url.searchParams.get('path')||'');
        send(res,200,fs.readFileSync(resource.absolute),resource.mime,{'Content-Length':resource.size});return;
      }
      if(req.method==='POST'&&url.pathname==='/api/session/start'){const input=await body(req);json(res,200,await controller.start(modeOf(input.mode,controller.snapshot().mode)));return;}
      if(req.method==='POST'&&url.pathname==='/api/session/restart'){const input=await body(req);json(res,200,await controller.restart(modeOf(input.mode,controller.snapshot().mode)));return;}
      if(req.method==='POST'&&url.pathname==='/api/session/stop'){await body(req);json(res,200,await controller.stop());return;}
      if(req.method==='POST'&&url.pathname==='/api/session/fault'){const input=await body(req);json(res,200,await controller.setFault(input.fault));return;}
      if(req.method==='POST'&&url.pathname.startsWith('/api/tools/')){
        const name=decodeURIComponent(url.pathname.slice('/api/tools/'.length));const input=await body(req);json(res,200,await controller.callTool(name,input));return;
      }
      json(res,404,{error:{code:'NOT_FOUND',message:'The requested console route does not exist.'}});
    }catch(error){const safe=stableError(error);json(res,error instanceof SyntaxError?400:422,{error:safe});}
  });
  const port=options.port??DEFAULT_PORT;
  await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(port,host,()=>{server.off('error',reject);resolve();});});
  const address=server.address() as AddressInfo;
  const close=async()=>{experience.close();await controller.close();await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()));};
  return {url:`http://${host}:${address.port}`,controller,server,close};
}

async function main():Promise<void>{
  const here=path.dirname(fileURLToPath(import.meta.url));
  const repoRoot=process.env.TENNIS_REPO_ROOT||path.resolve(here,'../../../..');
  const handle=await startDebugConsole({repoRoot,dataRoot:process.env.TENNIS_DEBUG_DATA_ROOT,port:process.env.TENNIS_DEBUG_PORT?Number(process.env.TENNIS_DEBUG_PORT):DEFAULT_PORT});
  process.stdout.write(`Tennis story workbench: ${handle.url}\n`);
  let closing=false;const close=async()=>{if(closing)return;closing=true;await handle.close();};
  for(const signal of ['SIGINT','SIGTERM'] as const)process.once(signal,()=>{void close().finally(()=>process.exit(0));});
}

if(process.argv[1]&&import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href)main().catch(error=>{const safe=stableError(error);process.stderr.write(JSON.stringify(safe)+'\n');process.exitCode=1;});
