import fs from 'node:fs';
import type {IncomingMessage,ServerResponse} from 'node:http';
import path from 'node:path';
import os from 'node:os';
import {randomUUID} from 'node:crypto';
import {spawn,type ChildProcess} from 'node:child_process';
import {pipeline} from 'node:stream/promises';
import {Transform} from 'node:stream';
import {experiencePage,experienceStyles} from './video-experience-ui.js';
import {experienceScript} from './video-experience-client.js';
import {videoReport} from './video-experience-report.js';

const MAX_UPLOAD=512*1024*1024;
type Stage='inspect'|'pose'|'evidence';
type JobStatus='uploaded'|'running'|'completed'|'failed'|'cancelled'|'interrupted';
interface VideoInput {filename:string;age_band:'adult'|'youth';guardian:boolean;handedness:'right'|'left';stroke:string;goal:string;background:string;camera:string;safety:string;consent:boolean;save:boolean;retest:boolean;}
interface Job extends VideoInput {id:string;created_at:string;status:JobStatus;stage:Stage|null;completed:Stage[];error:string|null;size:number;simulation:false;}
const STROKES=new Set(['forehand','single_backhand','double_backhand','serve','forehand_volley','backhand_volley','footwork']);
const ERRORS:Record<string,string>={
  INVALID_INPUT:'请填写年龄段、惯用手、目标动作、拍摄机位、训练经历和本次目标，并同意本次分析。',
  GUARDIAN_REQUIRED:'青少年视频需要监护人确认后上传。', SAFETY_STOP:'当前疼痛提示不适合继续动作分析。请先处理身体不适。',
  FILE_TOO_LARGE:'视频超过 512 MB，请压缩或裁剪后重试。', FILE_EMPTY:'文件为空，请重新选择视频。',
  MEDIA_UNSUPPORTED:'视频无法读取、格式不兼容或为原生全景文件。请导出平面 MP4/MOV 后重试。',
  DURATION_EXCEEDED:'视频超过 180 秒，请裁剪后重新上传。', VIDEO_RESOURCE_LIMIT:'请导出不超过 4K、240 fps 的视频后重试。',
  VIDEO_CONVERSION_FAILED:'视频无法转换为本地可播放副本。请导出 H.264 MP4 后重试。', VIDEO_DECODE_FAILED:'无法解码视频画面，请重新导出 MP4。',
  BUSY:'另一个视频正在处理，请完成或取消后再试。', INSPECTION_REQUIRED:'请先完成视频检查。', POSE_REQUIRED:'请先完成人体姿态检测。',
  POSE_MODEL_UNAVAILABLE:'本地姿态模型未就绪或校验失败。', JOB_NOT_FOUND:'未找到该视频任务。',
  VIDEO_PROCESSING_FAILED:'本次处理未完成，已保留视频和已完成步骤，可以重试。', PROCESS_TIMEOUT:'处理超时，已保留视频，可以重试。',
  RESOURCE_UNAVAILABLE:'该资源尚未生成或不可访问。', UPLOAD_CANCELLED:'上传已取消，可以重新选择视频。', INVALID_RANGE:'播放范围无效，请重新加载视频。',
  ALREADY_RUNNING:'当前任务正在处理中。', STAGE_UNKNOWN:'请求的处理步骤不可用。', PATH_FORBIDDEN:'无法访问该路径。',
  DELETE_CONFIRMATION_REQUIRED:'请明确确认要删除的本地视频任务。',
};
class ExperienceError extends Error {constructor(readonly code:string,readonly status=422){super(ERRORS[code]||ERRORS.VIDEO_PROCESSING_FAILED);}}
function sendJson(res:ServerResponse,status:number,value:unknown):void {res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(value));}
function read<T>(location:string,fallback:T):T {try{return JSON.parse(fs.readFileSync(location,'utf8')) as T;}catch{return fallback;}}
function write(location:string,value:unknown):void {fs.mkdirSync(path.dirname(location),{recursive:true,mode:0o700});fs.writeFileSync(location+'.tmp',JSON.stringify(value),{mode:0o600});fs.renameSync(location+'.tmp',location);}
function runner():string {for(const candidate of [path.join(os.homedir(),'.local/bin/uv'),'/opt/homebrew/bin/uv',...String(process.env.PATH||'').split(path.delimiter).map(p=>path.join(p,'uv'))]){try{fs.accessSync(candidate,fs.constants.X_OK);return candidate;}catch{}}throw new ExperienceError('POSE_MODEL_UNAVAILABLE');}
function input(value:unknown):VideoInput {
  if(!value||typeof value!=='object')throw new ExperienceError('INVALID_INPUT');
  const p=value as Record<string,unknown>;
  if(!['adult','youth'].includes(String(p.age_band))||!['left','right'].includes(String(p.handedness))||!STROKES.has(String(p.stroke))||!['front','side','rear','unknown'].includes(String(p.camera))||typeof p.goal!=='string'||!p.goal.trim()||p.goal.length>500||typeof p.background!=='string'||!p.background.trim()||p.background.length>500||p.consent!==true||!['clear','pain'].includes(String(p.safety)))throw new ExperienceError('INVALID_INPUT');
  if(p.age_band==='youth'&&p.guardian!==true)throw new ExperienceError('GUARDIAN_REQUIRED');
  if(p.safety!=='clear')throw new ExperienceError('SAFETY_STOP');
  const filename=typeof p.filename==='string'?path.basename(p.filename).replace(/[\x00-\x1f]/g,'').slice(0,180):'';
  if(!/\.(mp4|mov)$/i.test(filename))throw new ExperienceError('MEDIA_UNSUPPORTED');
  return {filename,age_band:p.age_band as VideoInput['age_band'],guardian:p.guardian===true,handedness:p.handedness as VideoInput['handedness'],stroke:String(p.stroke),goal:p.goal.trim(),background:p.background.trim(),camera:String(p.camera),safety:'clear',consent:true,save:p.save===true,retest:p.retest===true};
}

export class VideoExperience {
  private active:{id:string;child:ChildProcess;timer:ReturnType<typeof setTimeout>}|null=null;
  private readonly jobs=new Map<string,Job>();
  readonly root:string;
  constructor(private readonly repoRoot:string,root?:string){
    this.root=path.resolve(root||path.join(repoRoot,'tmp/video-experience'));
    fs.mkdirSync(this.root,{recursive:true,mode:0o700});
    for(const name of fs.readdirSync(this.root)){if(!/^video-[a-f0-9-]{36}$/.test(name))continue;const job=read<Job|null>(path.join(this.root,name,'job.json'),null);if(job?.id===name){if(job.status==='running'){job.status='interrupted';job.error='VIDEO_PROCESSING_FAILED';this.save(job);}this.jobs.set(name,job);}}
  }
  private job(id:string):Job {const job=this.jobs.get(id);if(!job)throw new ExperienceError('JOB_NOT_FOUND',404);return job;}
  private dir(id:string):string {const location=path.join(this.root,this.job(id).id);if(!fs.realpathSync(location).startsWith(fs.realpathSync(this.root)+path.sep))throw new ExperienceError('PATH_FORBIDDEN',403);return location;}
  private save(job:Job):void {write(path.join(this.root,job.id,'job.json'),job);}
  private data(id:string,name:string):unknown {try{return read<unknown>(this.resource(id,name),null);}catch{return null;}}
  snapshot(id:string):unknown {const job=this.job(id);const root=this.dir(id);const progress=job.status==='running'?read<Record<string,unknown>>(path.join(root,'progress.json'),{}):{};return {...job,progress,result:this.data(id,'result.json')};}
  list():unknown[] {return [...this.jobs.values()].sort((a,b)=>b.created_at.localeCompare(a.created_at)).map(job=>({...job,result:this.data(job.id,'result.json')}));}
  async upload(req:IncomingMessage):Promise<unknown>{
    const header=req.headers['x-video-metadata'];if(typeof header!=='string'||header.length>12000)throw new ExperienceError('INVALID_INPUT');
    let parameters:VideoInput;try{parameters=input(JSON.parse(Buffer.from(header,'base64url').toString('utf8')));}catch(error){if(error instanceof ExperienceError)throw error;throw new ExperienceError('INVALID_INPUT');}
    const declared=Number(req.headers['content-length']||0);if(declared>MAX_UPLOAD)throw new ExperienceError('FILE_TOO_LARGE',413);
    const id='video-'+randomUUID();const directory=path.join(this.root,id);fs.mkdirSync(directory,{mode:0o700});
    let size=0;const limiter=new Transform({transform(chunk:Buffer,_encoding,callback){size+=chunk.length;if(size>MAX_UPLOAD)callback(new ExperienceError('FILE_TOO_LARGE',413));else callback(null,chunk);}});
    try{await pipeline(req,limiter,fs.createWriteStream(path.join(directory,'source.part'),{mode:0o600}));if(size===0)throw new ExperienceError('FILE_EMPTY');fs.renameSync(path.join(directory,'source.part'),path.join(directory,'source'));}
    catch(error){fs.rmSync(directory,{recursive:true,force:true});throw error;}
    const job:Job={...parameters,id,created_at:new Date().toISOString(),status:'uploaded',stage:null,completed:[],error:null,size,simulation:false};this.jobs.set(id,job);this.save(job);return this.snapshot(id);
  }
  start(id:string,stage:Stage):unknown {
    const job=this.job(id);if(this.active)throw new ExperienceError(this.active.id===id?'ALREADY_RUNNING':'BUSY',409);
    if(!['inspect','pose','evidence'].includes(stage))throw new ExperienceError('STAGE_UNKNOWN');
    if(stage!=='inspect'&&!job.completed.includes('inspect'))throw new ExperienceError('INSPECTION_REQUIRED');
    if(stage==='evidence'&&!job.completed.includes('pose'))throw new ExperienceError('POSE_REQUIRED');
    if(job.completed.includes(stage))return this.snapshot(id);
    const executable=runner();const root=this.dir(id);job.status='running';job.stage=stage;job.error=null;this.save(job);
    write(path.join(root,'progress.json'),{status:'running',stage,progress:0,message:'正在准备本地处理'});
    const child=spawn(executable,['run','--frozen','--offline','--project',path.join(this.repoRoot,'local-backend'),'python','-m','tennis_backend.experience.runner','--job-root',root,'--stage',stage,'--model',path.join(this.repoRoot,'models/pose/pose_landmarker_full.task')],{cwd:this.repoRoot,stdio:['ignore','ignore','ignore'],detached:process.platform!=='win32',env:{PATH:process.env.PATH,HOME:os.homedir(),LANG:'en_US.UTF-8',OMP_NUM_THREADS:'2'}});
    const timer=setTimeout(()=>this.stop(id,'PROCESS_TIMEOUT'),8*60*1000);timer.unref();this.active={id,child,timer};
    const finish=(code:number|null)=>{if(this.active?.child!==child)return;clearTimeout(timer);this.active=null;const progress=read<{status?:string;error?:string}>(path.join(root,'progress.json'),{});if(code===0&&progress.status==='completed'){job.status='completed';if(!job.completed.includes(stage))job.completed.push(stage);}else{job.status='failed';job.error=progress.error||'VIDEO_PROCESSING_FAILED';}this.save(job);};
    child.once('error',()=>finish(1));child.once('exit',finish);return this.snapshot(id);
  }
  private stop(id:string,error:string|null=null):void {const job=this.job(id);const current=this.active;if(current?.id===id){this.active=null;clearTimeout(current.timer);try{if(current.child.pid&&process.platform!=='win32')process.kill(-current.child.pid,'SIGTERM');else current.child.kill('SIGTERM');}catch{}job.status=error?'failed':'cancelled';job.error=error;this.save(job);}}
  cancel(id:string):unknown {this.stop(id);return this.snapshot(id);}
  async remove(id:string,confirmation:unknown):Promise<unknown> {if(confirmation!==id)throw new ExperienceError('DELETE_CONFIRMATION_REQUIRED');const child=this.active?.id===id?this.active.child:null;this.stop(id);if(child&&child.exitCode===null&&child.signalCode===null)await new Promise<void>(resolve=>{const timer=setTimeout(()=>{try{if(child.pid)process.kill(-child.pid,'SIGKILL');}catch{}resolve();},3000);child.once('exit',()=>{clearTimeout(timer);resolve();});});fs.rmSync(this.dir(id),{recursive:true,force:true});this.jobs.delete(id);return {id,deleted:true,scope:'local_video_and_derivatives'};}
  private resource(id:string,name:string):string {if(!/^(preview\.mp4|poster\.jpg|pose\.json|result\.json|evidence-\d{1,2}\.jpg)$/.test(name))throw new ExperienceError('PATH_FORBIDDEN',403);const root=fs.realpathSync(this.dir(id));const location=path.join(root,name);if(!fs.existsSync(location)||!fs.realpathSync(location).startsWith(root+path.sep))throw new ExperienceError('RESOURCE_UNAVAILABLE',404);return location;}
  private stream(req:IncomingMessage,res:ServerResponse,id:string,name:string):void {
    const location=this.resource(id,name);const size=fs.statSync(location).size;const mime=name.endsWith('.mp4')?'video/mp4':name.endsWith('.jpg')?'image/jpeg':'application/json';let start=0,end=size-1,status=200;
    if(req.headers.range){const range=/^bytes=(\d+)-(\d*)$/.exec(req.headers.range);if(!range)throw new ExperienceError('INVALID_RANGE',416);start=Number(range[1]);end=range[2]?Number(range[2]):size-1;if(start>=size||end<start||end>=size)throw new ExperienceError('INVALID_RANGE',416);status=206;}
    res.writeHead(status,{'Content-Type':mime,'Content-Length':end-start+1,'Accept-Ranges':'bytes','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff',...(status===206?{'Content-Range':`bytes ${start}-${end}/${size}`}:{})});const stream=fs.createReadStream(location,{start,end});res.once('close',()=>stream.destroy());stream.pipe(res);
  }
  async route(req:IncomingMessage,res:ServerResponse,url:URL):Promise<boolean>{
    const pathname=url.pathname;
    if(pathname==='/'||pathname==='/experience.css'||pathname==='/experience.js'){
      if(req.method!=='GET')return false;const content=pathname==='/'?experiencePage:pathname.endsWith('.css')?experienceStyles:experienceScript;
      res.writeHead(200,{'Content-Type':pathname==='/'?'text/html; charset=utf-8':pathname.endsWith('.css')?'text/css; charset=utf-8':'text/javascript; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"});res.end(content);return true;
    }
    if(pathname.startsWith('/icons/')){const name=pathname.slice(7);if(!/^[a-z][a-z0-9-]*\.svg$/.test(name))return false;const location=path.join(this.repoRoot,'workbuddy-plugin/bridge/node_modules/lucide-static/icons',name);if(!fs.existsSync(location))return false;res.writeHead(200,{'Content-Type':'image/svg+xml','Cache-Control':'public, max-age=3600'});res.end(fs.readFileSync(location));return true;}
    if(!pathname.startsWith('/api/experience'))return false;
    try{
      if(pathname==='/api/experience/videos'&&req.method==='GET'){sendJson(res,200,{items:this.list()});return true;}
      if(pathname==='/api/experience/videos'&&req.method==='POST'){sendJson(res,201,await this.upload(req));return true;}
      const match=/^\/api\/experience\/videos\/(video-[a-f0-9-]{36})(?:\/(inspect|pose|evidence|cancel|export|report|resources\/[^/]+))?$/.exec(pathname);if(!match){sendJson(res,404,{error:{code:'JOB_NOT_FOUND',message:ERRORS.JOB_NOT_FOUND}});return true;}
      const id=match[1]!;const action=match[2];
      if(req.method==='GET'&&!action){sendJson(res,200,this.snapshot(id));return true;}
      if(req.method==='DELETE'&&!action){sendJson(res,200,await this.remove(id,req.headers['x-confirm-video']));return true;}
      if(req.method==='GET'&&action?.startsWith('resources/')){this.stream(req,res,id,action.slice(10));return true;}
      if(req.method==='GET'&&action==='report'){
        const job=this.job(id);const result=this.data(id,'result.json') as {media?:{duration_s:number;width:number;height:number;codec:string};source_sha256?:string;model?:{sha256:string};pose?:{summary:Parameters<typeof videoReport>[0]['summary']};evidence?:{file:string;t:number}[]}|null;
        const evidence=(result?.evidence||[]).map(item=>({t:item.t,jpeg:fs.readFileSync(this.resource(id,item.file)).toString('base64')}));
        const report=videoReport({filename:job.filename,stroke:job.stroke,handedness:job.handedness,created_at:job.created_at,media:result?.media||{},summary:result?.pose?.summary||{},sourceHash:result?.source_sha256||'',modelHash:result?.model?.sha256||'',evidence});
        res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Content-Disposition':'attachment; filename="tennis-video-review.html"','Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; frame-ancestors 'none'"});res.end(report);return true;
      }
      if(req.method==='GET'&&action==='export'){const job=this.job(id);const report={version:'real-video-1',simulation:false,video:{filename:job.filename,stroke:job.stroke,handedness:job.handedness,camera:job.camera},completed:job.completed,result:this.data(id,'result.json'),pose:this.data(id,'pose.json')};res.setHeader('Content-Disposition','attachment; filename="tennis-video-analysis.json"');sendJson(res,200,report);return true;}
      if(req.method==='POST'&&action==='cancel'){sendJson(res,200,this.cancel(id));return true;}
      if(req.method==='POST'&&action&&['inspect','pose','evidence'].includes(action)){sendJson(res,202,this.start(id,action as Stage));return true;}
      throw new ExperienceError('STAGE_UNKNOWN');
    }catch(error){if(res.headersSent){res.destroy();return true;}const safe=error instanceof ExperienceError?error:new ExperienceError('VIDEO_PROCESSING_FAILED');sendJson(res,safe.status,{error:{code:safe.code,message:safe.message}});return true;}
  }
  close():void {if(this.active)this.stop(this.active.id);for(const job of this.jobs.values()){if(!job.save){fs.rmSync(this.dir(job.id),{recursive:true,force:true});this.jobs.delete(job.id);}}}
}
