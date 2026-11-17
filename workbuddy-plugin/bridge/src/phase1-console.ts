import fs from 'node:fs';
import http,{type IncomingMessage,type ServerResponse} from 'node:http';
import path from 'node:path';
import type {AddressInfo} from 'node:net';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {Phase1Backend,Phase1Error,createFixtureParams} from './phase1-backend.js';
import {phase1Page,phase1Styles} from './phase1-console-ui.js';
import {phase1Script} from './phase1-console-client.js';
import {Phase1Preview} from './phase1-preview.js';
import {Phase2Media,MAX_UPLOAD} from './phase2-media.js';
import {phase2Script} from './phase2-client.js';
import {Phase3Media} from './phase3-media.js';
import {phase3Script} from './phase3-client.js';
import {phase3PlaybackScript} from './phase3-playback.js';
import {actionSelectionScript} from './action-selection-client.js';
import {phase4Script} from './phase4-client.js';
import {Phase4Media} from './phase4-media.js';
import {Phase4Service} from './phase4-service.js';
import {CONTRACT_VERSION,ContractRegistry} from './contracts.js';
import {Phase5Service} from './phase5-service.js';
import {phase5Script} from './phase5-client.js';
import {ExpertPolicy,ExpertError} from './expert-policy.js';
import {ExpertRuntime} from './expert-runtime.js';
import {ExpertService} from './expert-service.js';
import {expertScript} from './expert-panel.js';
import {ExpertContext} from './expert-context.js';
import {ExpertStore} from './expert-store.js';

const MAX_BODY=2*1024*1024;
const SECURITY={
  'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
  'Cross-Origin-Resource-Policy':'same-origin',
  'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
};
function json(res:ServerResponse,status:number,value:unknown){res.writeHead(status,{...SECURITY,'Content-Type':'application/json; charset=utf-8'});res.end(JSON.stringify(value));}
async function body(req:IncomingMessage):Promise<Record<string,unknown>>{
  let size=0;const chunks:Buffer[]=[];
  for await(const chunk of req){const b=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);size+=b.length;if(size>MAX_BODY)throw new Phase1Error('REQUEST_TOO_LARGE',413);chunks.push(b);}
  let value:unknown;try{value=JSON.parse(Buffer.concat(chunks).toString('utf8')||'{}');}catch{throw new Phase1Error('HTTP_BODY_INVALID_JSON',400);}
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Phase1Error('HTTP_BODY_INVALID_JSON',400);return value as Record<string,unknown>;
}
function stream(req:IncomingMessage,res:ServerResponse,file:string){
  const size=fs.statSync(file).size;let start=0,end=size-1,status=200;
  if(req.headers.range){const match=/^bytes=(\d+)-(\d*)$/.exec(req.headers.range);if(!match){res.writeHead(416,{...SECURITY,'Content-Range':`bytes */${size}`});res.end();return;}
    start=Number(match[1]);end=match[2]?Math.min(Number(match[2]),size-1):size-1;
    if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start>=size||start>end){res.writeHead(416,{...SECURITY,'Content-Range':`bytes */${size}`});res.end();return;}status=206;
  }
  res.writeHead(status,{...SECURITY,'Content-Type':file.endsWith('.mp4')?'video/mp4':'application/x-ndjson','Content-Length':end-start+1,'Accept-Ranges':'bytes',...(status===206?{'Content-Range':`bytes ${start}-${end}/${size}`}:{})});
  const input=fs.createReadStream(file,{start,end});input.on('error',()=>res.destroy());res.on('close',()=>input.destroy());input.pipe(res);
}

export async function startPhase1Console(options:{repoRoot:string;port?:number;host?:string}){
  if(options.host&&options.host!=='127.0.0.1')throw new Phase1Error('LOOPBACK_ONLY');
  const repoRoot=path.resolve(options.repoRoot);const backend=new Phase1Backend(repoRoot);
  const previews=new Phase1Preview(path.join(repoRoot,'tmp/phase1-video-cache'));
  const mediaStore=new Phase2Media(repoRoot,()=>backend.snapshot().data_root);
  const poseStore=new Phase3Media(()=>backend.snapshot().data_root);
  const registry=new ContractRegistry(path.join(repoRoot,'contracts/v1'));
  const phase4Media=new Phase4Media(()=>backend.snapshot().data_root,registry);
  const phase4=new Phase4Service(phase4Media,registry,line=>backend.request(line));
  const phase5=new Phase5Service(()=>backend.snapshot().data_root,registry,line=>backend.request(line));
  const expertContext=new ExpertContext(()=>backend.snapshot().data_root,phase4,registry);
  const experts=new ExpertService(repoRoot,undefined,undefined,{context:expertContext,store:new ExpertStore(()=>backend.snapshot().data_root)}),expertPolicy=new ExpertPolicy();
  const runtimeSupport=new ExpertRuntime(backend,expertPolicy),supportIdentity=expertPolicy.bind('tennis-runtime-support');
  const validationRoot=path.join(repoRoot,'005 后端重构/isolation');
  const validationDir=(kind:string)=>kind==='sample'?path.join(validationRoot,'sample/validation-output'):kind==='empty'?path.join(validationRoot,'validation-output'):null;
  const validationFile=(kind:string,name:string)=>{
    const dir=validationDir(kind);if(!dir||!['annotated.mp4','skeleton.mp4','frames.jsonl','summary.json'].includes(name))throw new Phase1Error('RESOURCE_NOT_FOUND',404);
    const file=path.join(dir,name);if(!fs.existsSync(file)||!fs.realpathSync(file).startsWith(fs.realpathSync(dir)+path.sep))throw new Phase1Error('RESOURCE_NOT_FOUND',404);return file;
  };
  const server=http.createServer(async(req,res)=>{
    try{
      const host=req.headers.host||'';if(!/^(127\.0\.0\.1|localhost)(:\d+)?$/.test(host))throw new Phase1Error('LOCAL_REQUEST_REQUIRED',403);
      if(req.headers.origin){let origin:URL;try{origin=new URL(req.headers.origin);}catch{throw new Phase1Error('LOCAL_REQUEST_REQUIRED',403);}
        const own=new URL('http://'+host);if(origin.protocol!=='http:'||!['127.0.0.1','localhost'].includes(origin.hostname)||origin.port!==own.port)throw new Phase1Error('LOCAL_REQUEST_REQUIRED',403);
      }
      const url=new URL(req.url||'/','http://'+host);
      if(req.method==='GET'&&['/','/phase1.css','/phase1.js'].includes(url.pathname)){
        const content=url.pathname==='/'?phase1Page:url.pathname.endsWith('.css')?phase1Styles:[phase1Script,actionSelectionScript,phase2Script,phase3PlaybackScript,phase3Script,phase4Script,phase5Script,expertScript].join('\n');
        res.writeHead(200,{...SECURITY,'Content-Type':url.pathname==='/'?'text/html; charset=utf-8':url.pathname.endsWith('.css')?'text/css; charset=utf-8':'text/javascript; charset=utf-8'});res.end(content);return;
      }
      if(req.method==='GET'&&url.pathname==='/favicon.ico'){res.writeHead(204,SECURITY);res.end();return;}
      if(req.method==='GET'&&/^\/icons\/[a-z][a-z0-9-]*\.svg$/.test(url.pathname)){
        const icon=path.join(repoRoot,'workbuddy-plugin/bridge/node_modules/lucide-static/icons',path.basename(url.pathname));
        if(!fs.existsSync(icon))throw new Phase1Error('RESOURCE_NOT_FOUND',404);
        res.writeHead(200,{...SECURITY,'Content-Type':'image/svg+xml'});res.end(fs.readFileSync(icon));return;
      }
      if(req.method==='GET'&&url.pathname==='/api/phase1/state'){json(res,200,backend.snapshot());return;}
      if(url.pathname.startsWith('/api/experts/')){
        const route=url.pathname.slice('/api/experts/'.length);
        if(req.method==='GET'){
          if(route==='config'){json(res,200,experts.config());return;}
          if(route==='run'){json(res,200,experts.get(url.searchParams.get('id')||''));return;}
          if(route==='frame'){const file=experts.frame(url.searchParams.get('run_id')||'',url.searchParams.get('evidence_id')||'');res.writeHead(200,{...SECURITY,'Content-Type':'image/png','Content-Length':fs.statSync(file).size});fs.createReadStream(file).on('error',()=>res.destroy()).pipe(res);return;}
          if(route==='runtime'){json(res,200,await runtimeSupport.inspect(supportIdentity));return;}
        }
        if(req.method==='POST'){
          const input=await body(req);let result:unknown;
          const allowed:Record<string,string[]>={preview:['question','goal','handedness','background','safety'],'video-context':['workspace_ref'],'video-preview':['context_id','workspace_ref','selected_frame_ids','subject_confirmed','question','goal','handedness','background','safety'],start:['preview_id','confirmed'],cancel:['id'],models:[],'runtime/repair':[],'runtime/config':['control_timeout_ms']};
          if(!allowed[route])throw new ExpertError('NOT_FOUND',404);
          if(Object.keys(input).some(k=>!allowed[route]!.includes(k)))throw new ExpertError('EXPERT_INPUT_FIELD_DENIED',403);
          switch(route){
            case 'preview':result=experts.preview(input);break;
            case 'video-context':result=await experts.videoContext(String(input.workspace_ref||''));break;
            case 'video-preview':result=experts.videoPreview(input);break;
            case 'start':result=experts.start(input.preview_id,input.confirmed);break;
            case 'cancel':result=experts.cancel(String(input.id||''));break;
            case 'models':result=await experts.client.models();break;
            case 'runtime/repair':result=await runtimeSupport.repair(supportIdentity);break;
            case 'runtime/config':result=runtimeSupport.configure(supportIdentity,input);break;
          }
          json(res,200,result);return;
        }
        throw new ExpertError('NOT_FOUND',404);
      }
      if(req.method==='GET'&&url.pathname==='/api/phase5/tasks'){json(res,200,phase5.tasks());return;}
      if(req.method==='GET'&&url.pathname==='/api/phase5/subjects'){json(res,200,phase5.subjects(url.searchParams.get('workspace_ref')||''));return;}
      if(req.method==='GET'&&url.pathname==='/api/phase5/active'){json(res,200,phase5.active());return;}
      if(req.method==='GET'&&url.pathname==='/api/phase5/inspect'){json(res,200,phase5.inspect(url.searchParams.get('workspace_ref')||''));return;}
      if(req.method==='GET'&&url.pathname==='/api/phase5/job'){json(res,200,phase5.job(url.searchParams.get('id')||''));return;}
      if(req.method==='POST'&&url.pathname.startsWith('/api/phase5/')){const input=await body(req),w=String(input.workspace_ref||'');let result:unknown;switch(url.pathname){case '/api/phase5/reselect':result=phase5.reselect(w,input.subject_id,input.confirmed);break;case '/api/phase5/create':result=phase5.start(input);break;case '/api/phase5/control':result=await phase5.control(w,String(input.action||''),String(input.other_workspace||''));break;case '/api/phase5/export':result=await phase5.export(w,input.selected_resource_ids,input.confirmed);break;case '/api/phase5/delete-preview':experts.assertWorkspaceIdle(w);result=phase5.prepareDelete(w);break;case '/api/phase5/delete':experts.assertWorkspaceIdle(w);result=await phase5.delete(w,String(input.token||''),String(input.confirmation||''));break;default:throw new Phase1Error('NOT_FOUND',404);}json(res,200,result);return;}
      if(req.method==='GET'&&url.pathname==='/phase5/resource'){const file=phase5.resource(url.searchParams.get('token')||'');res.writeHead(200,{...SECURITY,'Content-Type':'application/json','Content-Length':fs.statSync(file).size,'Content-Disposition':'attachment'});fs.createReadStream(file).on('error',()=>res.destroy()).pipe(res);return;}
      if(req.method==='POST'&&url.pathname==='/api/phase4/query'){const input=await body(req);json(res,200,await phase4.query(String(input.workspace_ref||'')));return;}
      if(req.method==='POST'&&url.pathname==='/api/phase4/render'){const input=await body(req);json(res,200,await phase4.render(String(input.workspace_ref||''),String(input.kind||''),input.subject_id===undefined?undefined:String(input.subject_id)));return;}
      if(req.method==='GET'&&url.pathname==='/phase4/resource'){const resource=phase4Media.resource(url.searchParams.get('token')||'');if(resource.mime==='image/png'){res.writeHead(200,{...SECURITY,'Content-Type':'image/png','Content-Length':fs.statSync(resource.file).size});fs.createReadStream(resource.file).on('error',()=>res.destroy()).pipe(res);return;}stream(req,res,resource.file);return;}
      if(req.method==='GET'&&url.pathname==='/api/phase1/config'){
        json(res,200,{default_data_root:path.join(repoRoot,'tmp/phase1-workbench-data'),contract_version:CONTRACT_VERSION,create_params:createFixtureParams('op-example')});return;
      }
      if(req.method==='POST'&&url.pathname==='/api/phase1/start'){
        const input=await body(req);if(input.data_root!==null&&typeof input.data_root!=='string')throw new Phase1Error('DATA_ROOT_INVALID',400);
        json(res,200,await backend.start(input.data_root as string|null));return;
      }
      if(req.method==='POST'&&url.pathname==='/api/phase1/stop'){await body(req);json(res,200,await backend.stop());return;}
      if(req.method==='POST'&&url.pathname==='/api/phase1/request'){
        const input=await body(req);if(typeof input.line!=='string')throw new Phase1Error('ONE_LINE_REQUIRED',400);json(res,200,await backend.request(input.line));return;
      }
      if(req.method==='POST'&&url.pathname==='/api/phase2/upload'){
        if(!backend.running)throw new Phase1Error('BACKEND_NOT_RUNNING',409);
        if(Number(req.headers['content-length']||0)>MAX_UPLOAD)throw new Phase1Error('UPLOAD_TOO_LARGE',413);
        let name:string;try{name=decodeURIComponent(String(req.headers['x-file-name']||''));}catch{throw new Phase1Error('UPLOAD_NAME_INVALID',400);}
        json(res,201,await mediaStore.upload(req,name));return;
      }
      if(req.method==='POST'&&url.pathname==='/api/phase2/approved-sample'){
        await body(req);if(!backend.running)throw new Phase1Error('BACKEND_NOT_RUNNING',409);
        json(res,201,await mediaStore.approved());return;
      }
      if(req.method==='GET'&&url.pathname==='/api/phase2/artifacts'){
        json(res,200,mediaStore.result(url.searchParams.get('workspace_ref')||''));return;
      }
      if(req.method==='GET'&&url.pathname==='/api/phase3/tasks'){json(res,200,{items:poseStore.tasks()});return;}
      if(req.method==='GET'&&url.pathname==='/api/phase3/selection'){const workspace=url.searchParams.get('workspace_ref')||'';json(res,200,{selection:poseStore.selection(workspace),segments:poseStore.segments(workspace),availability:poseStore.availability(workspace)});return;}
      if(req.method==='GET'&&url.pathname==='/api/phase3/result'){json(res,200,poseStore.result(url.searchParams.get('workspace_ref')||''));return;}
      if(req.method==='GET'&&url.pathname==='/phase3/artifact'){
        const kind=url.searchParams.get('kind')||'';if(!['video','pose','spec','selection','segments'].includes(kind))throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
        const file=poseStore.artifact(url.searchParams.get('workspace_ref')||'',kind);
        if(kind==='spec'||kind==='selection'||kind==='segments'){json(res,200,JSON.parse(fs.readFileSync(file,'utf8')));return;}
        stream(req,res,file);return;
      }
      if(req.method==='GET'&&url.pathname==='/phase2/artifact'){
        const kind=url.searchParams.get('kind')||'',file=mediaStore.artifact(url.searchParams.get('workspace_ref')||'',kind);
        if(!['video','frames','selection'].includes(kind))throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
        if(kind==='selection'){json(res,200,JSON.parse(fs.readFileSync(file,'utf8')));return;}
        stream(req,res,kind==='video'?await previews.file(file):file);return;
      }
      if(req.method==='GET'&&url.pathname==='/api/phase1/validation'){
        const items=['sample','empty'].map(kind=>{
          try{return {id:kind,label:kind==='sample'?'真实网球样本':'无人合成反例',summary:JSON.parse(fs.readFileSync(validationFile(kind,'summary.json'),'utf8')),files:Object.fromEntries(['annotated.mp4','skeleton.mp4','frames.jsonl'].map(name=>{try{validationFile(kind,name);return [name,`/validation/${kind}/${name}`];}catch{return [name,null];}}))};}
          catch{return {id:kind,label:kind==='sample'?'真实网球样本':'无人合成反例',summary:null,files:{}};}
        });json(res,200,{items});return;
      }
      const media=/^\/validation\/(sample|empty)\/(annotated\.mp4|skeleton\.mp4|frames\.jsonl)$/.exec(url.pathname);
      if(req.method==='GET'&&media){
        const original=validationFile(media[1]!,media[2]!);
        const file=media[2]!.endsWith('.mp4')?await previews.file(original):original;
        stream(req,res,file);return;
      }
      throw new Phase1Error('NOT_FOUND',404);
    }catch(error){
      if(res.headersSent){res.destroy();return;}
      const safe=error instanceof Phase1Error||error instanceof ExpertError?error:new Phase1Error('CONSOLE_OPERATION_FAILED',500);json(res,safe.status,{error:{code:safe.code}});
    }
  });
  await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(options.port??4180,'127.0.0.1',()=>{server.off('error',reject);resolve();});});
  let closing:Promise<void>|null=null;
  const close=()=>closing??=(async()=>{experts.close();await backend.stop();server.closeAllConnections();await new Promise<void>((resolve,reject)=>server.close(e=>e?reject(e):resolve()));})();
  return {url:`http://127.0.0.1:${(server.address() as AddressInfo).port}`,server,backend,close};
}

async function main(){
  const repoRoot=process.env.TENNIS_REPO_ROOT||path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../../..');
  const port=Number(process.env.TENNIS_PHASE1_PORT||4180);
  const handle=await startPhase1Console({repoRoot,port});process.stdout.write(`Phase 1 workbench: ${handle.url}\n`);
  for(const signal of ['SIGINT','SIGTERM'] as const)process.once(signal,()=>{void handle.close().finally(()=>process.exit(0));});
}
if(process.argv[1]&&import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href)main().catch(()=>{process.stderr.write('PHASE1_CONSOLE_START_FAILED\n');process.exitCode=1;});
