import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {ContractRegistry,type RequestEnvelope,type ResponseEnvelope,type WireError,type JsonObject} from './contracts.js';
import {PathBoundary} from './paths.js';

interface Sample {
  fixture_id:string;duration_s:number;stroke_kind:string;visible_objects:string[];subject_candidates:string[];
  requires_subject_selection:boolean;mirror_conflict:boolean;expected_state:string;expected_quality:string;issue_codes:string[];missing_objects:string[];device_scenario:string;
}
interface Consent {task_version:number;purposes:string[];confirmed:boolean;guardian_simulated:boolean;}
interface User {age_band:string;handedness:string;target_stroke:string;safety:string;}
interface CreateInput {fixture_id:string;media_ref:string;user:User;consent:Consent;subject_id:string|null;mirror_confirmed:boolean;primary_route:string;auxiliary_routes:string[];}
interface Resource {resource_id:string;path:string;mime:string;sha256:string;size_bytes:number;kind:string;simulation:boolean;}
interface FixtureTask {
  id:string;version:number;sample:Sample;input:CreateInput;state:string;checkpoints:string[];resources:Map<string,Resource>;timer?:NodeJS.Timeout;
}
interface FixtureOptions {fixtureRoot:string;dataRoot:string;assetRoot?:string;delayMs?:number;fault?:'timeout'|'crash'|'invalid_response'|'delete_partial';}
const fatal=new Set(['pain','injury','dizziness','worsening']);
function stable(value:unknown):unknown {
  if(Array.isArray(value))return value.map(stable);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([key,item])=>[key,stable(item)]));
  return value;
}

export class FixtureBackend {
  private readonly samples:Sample[];
  private readonly compatibility:{fixture_id:string;media:JsonObject}[];
  private readonly timing:{timing_order:string[];event_requires:Record<string,string[]>};
  private readonly tasks=new Map<string,FixtureTask>();
  private readonly operations=new Map<string,{fingerprint:string;response:ResponseEnvelope}>();
  private readonly boundary:PathBoundary;
  constructor(private readonly registry:ContractRegistry,private readonly options:FixtureOptions){
    const read=(name:string)=>JSON.parse(fs.readFileSync(path.join(options.fixtureRoot,name),'utf8')) as unknown;
    this.samples=(read('manifest.json') as {samples:Sample[]}).samples;
    this.compatibility=(read('expected/compatibility.json') as {cases:{fixture_id:string;media:JsonObject}[]}).cases;
    this.timing=read('expected/actions.json') as typeof this.timing;
    this.boundary=new PathBoundary(options.fixtureRoot,options.dataRoot);
  }
  async call(request:RequestEnvelope):Promise<ResponseEnvelope>{
    this.registry.request(request);
    if(this.options.fault==='crash')throw new Error('SIMULATED_BACKEND_EXIT');
    if(this.options.fault==='timeout')await new Promise(resolve=>setTimeout(resolve,200));
    if(this.options.fault==='invalid_response')return this.reply(request,{});
    const tool=this.registry.tool(request.method);
    const fingerprint=createHash('sha256').update(JSON.stringify(stable([request.method,request.params]))).digest('hex');
    const prior=request.operation_id?this.operations.get(request.operation_id):undefined;
    if(prior){
      if(prior.fingerprint!==fingerprint)return this.failure(request,'OPERATION_CONFLICT','input','confirm');
      if(request.method==='analysis.create'&&prior.response.ok){const id=(prior.response.result as {task_id:string}).task_id;const task=this.tasks.get(id);if(task)return this.reply(request,this.summary(task));}
      return {...prior.response,request_id:request.request_id};
    }
    let response:ResponseEnvelope;
    try{response=this.reply(request,this.perform(request.method,request.params));this.registry.response(tool.name,response);}
    catch(error){const code=error instanceof Error?error.message:'ANALYSIS_FAILED';const inputError=/CONSENT|GUARDIAN|SUBJECT|ROUTE|FIXTURE/.test(code);response=this.failure(request,code,inputError?'input':'analysis',inputError?'confirm':code==='RESOURCE_UNAVAILABLE'?'repair':'retry');}
    if(tool.side_effect&&request.operation_id&&response.ok)this.operations.set(request.operation_id,{fingerprint,response});
    return response;
  }
  close():void{for(const task of this.tasks.values())if(task.timer)clearTimeout(task.timer);}
  private reply(request:RequestEnvelope,result:unknown):ResponseEnvelope{return {contract_version:'1.1.0',request_id:request.request_id,ok:true,result,error:null,simulation:true};}
  private failure(request:RequestEnvelope,code:string,stage:string,action:string):ResponseEnvelope{
    const safe=/^[A-Z][A-Z0-9_]+$/.test(code)?code:'ANALYSIS_FAILED';
    const error:WireError={code:safe,stage,retryable:action==='retry',user_action:action,retained:[],details_ref:null,simulation:true};
    return {contract_version:'1.1.0',request_id:request.request_id,ok:false,result:null,error,simulation:true};
  }
  private perform(method:string,p:JsonObject):unknown {
    if(method==='analysis.create')return this.create(p as unknown as CreateInput);
    if(method==='analysis.delete')return this.remove(p);
    const task=this.get(p);
    if(method==='analysis.status')return this.summary(task);
    if(method==='analysis.cancel'){
      if(['checking_input','analyzing','waiting_for_capability','researching','rendering'].includes(task.state)){
        if(task.timer)clearTimeout(task.timer);task.state='cancelled';
      }
      return this.summary(task);
    }
    if(method==='analysis.quality')return this.quality(task);
    if(!['complete','partial'].includes(task.state))throw new Error('TASK_NOT_READY');
    if(method==='analysis.strokes')return {events:[{event_id:`event-${task.id}`,kind:task.sample.stroke_kind,start_s:0.2,end_s:Math.min(2.8,task.sample.duration_s),phases:['preparation','forward_swing','recovery'],confidence:'medium',evidence_ids:this.evidence(task).map(e=>e.evidence_id)}],simulation:true};
    if(method==='analysis.timing')return this.timeline(task);
    if(method==='analysis.metrics')return this.metrics(task);
    if(method==='evidence.render')return this.render(task,p);
    if(method==='target.generate')return this.target(task,p);
    if(method==='sessions.compare')return this.compare(task,p);
    if(method==='review.export')return this.export(task,p);
    throw new Error('METHOD_UNSUPPORTED');
  }
  private authorize(input:CreateInput,purpose:string,consent:Consent=input.consent):void {
    if(!consent.confirmed||consent.task_version!==1||!consent.purposes.includes(purpose))throw new Error('CONSENT_REQUIRED');
    if(input.user.age_band==='youth'&&!consent.guardian_simulated)throw new Error('GUARDIAN_CONFIRMATION_REQUIRED');
  }
  private create(input:CreateInput):unknown {
    this.authorize(input,'analysis');
    if(input.auxiliary_routes.includes(input.primary_route))throw new Error('ROUTE_CONFLICT');
    const sample=this.samples.find(s=>s.fixture_id===input.fixture_id);if(!sample)throw new Error('FIXTURE_UNKNOWN');
    if(input.subject_id&&!sample.subject_candidates.includes(input.subject_id))throw new Error('SUBJECT_UNKNOWN');
    const id=`task-${randomUUID()}`;
    const task:FixtureTask={id,version:1,sample,input,state:'checking_input',checkpoints:[],resources:new Map()};this.tasks.set(id,task);
    fs.mkdirSync(this.boundary.publicTask(`projects/${id}/v1`,id,1),{recursive:true});
    if(fatal.has(input.user.safety)||input.user.safety==='uncertain')task.state='safety_stop';
    else if(['unsupported','reshoot_required'].includes(sample.expected_state))task.state=sample.expected_state;
    else if((sample.requires_subject_selection&&!input.subject_id)||(sample.mirror_conflict&&!input.mirror_confirmed))task.state='checking_input';
    else{
      task.checkpoints.push('checking_input');task.state='analyzing';
      task.timer=setTimeout(()=>{if(task.state==='analyzing'){task.checkpoints.push('analyzing');task.state=sample.expected_state;}},this.options.delayMs??10);
    }
    return this.summary(task);
  }
  private get(p:JsonObject):FixtureTask {
    const task=this.tasks.get(String(p.task_id));if(!task||p.version!==task.version)throw new Error('TASK_NOT_FOUND');return task;
  }
  private summary(task:FixtureTask):JsonObject {
    return {task_id:task.id,version:task.version,state:task.state,stage:task.state==='analyzing'?'analysis':task.state==='checking_input'?'input':'none',checkpoints:[...task.checkpoints],retained:task.state==='deleted'?[]:['metadata',...task.resources.keys()],recovery_actions:task.state==='checking_input'?['ask']:['partial','reshoot_required'].includes(task.state)?['reshoot']:['none'],workspace_ref:`projects/${task.id}/v1`,simulation:true};
  }
  private evidence(task:FixtureTask):JsonObject[] {
    if(task.sample.fixture_id==='bad-time-mapping'||!['complete','partial','analyzing'].includes(task.state))return [];
    return [{evidence_id:`evidence-${task.id}`,kind:'video_frame',asset_id:task.sample.fixture_id,subject_id:task.input.subject_id||'person-1',original_start_s:1.2,original_end_s:1.2,visible_objects:task.sample.visible_objects,resource:[...task.resources.values()].find(r=>r.kind==='evidence_frame')||null,simulation:true}];
  }
  private quality(task:FixtureTask):JsonObject {
    const s=task.sample;
    return {status:s.expected_quality,issues:s.issue_codes.map(code=>({code,object:code.includes('feet')?'feet':code.includes('ball')?'ball':code.includes('racket')?'racket':code.includes('subject')?'subject':code.includes('direction')?'direction':'media',impact:'Requested observation is unavailable or needs confirmation in this synthetic scenario',evidence_ids:this.evidence(task).map(e=>e.evidence_id),reshoot_guidance:'Keep the full body, feet, incoming ball and racket visible; confirm subject/direction or provide a flat focused clip.'})),unavailable_objects:s.missing_objects,subject_candidates:s.subject_candidates,time_mapping_valid:s.fixture_id!=='bad-time-mapping',media:this.compatibility.find(x=>x.fixture_id===s.fixture_id)!.media,evidence:this.evidence(task),simulation:true};
  }
  private timeline(task:FixtureTask):JsonObject {
    const evidence=this.evidence(task).map(e=>e.evidence_id);
    return {events:this.timing.timing_order.map((kind,i)=>{
      const requires=this.timing.event_requires[kind]||[];const visible=requires.every(object=>task.sample.visible_objects.includes(object));
      return {event_id:`${task.id}-${kind}`,kind,original_s:visible?Number((0.2+i*0.23).toFixed(2)):null,value_state:visible?'observed':'not_visible',requires,evidence_ids:visible?evidence:[]};
    }),limitations:['Fixed synthetic event order; not calibrated event detection or a rigid stop-at-bounce rule'],simulation:true};
  }
  private metrics(task:FixtureTask):JsonObject {
    const metrics:JsonObject[]=[];const ids=this.evidence(task).map(e=>e.evidence_id);
    if(task.sample.visible_objects.includes('body'))metrics.push({metric_id:'preparation-order',definition:'Preparation relative to forward swing',value:'before_forward_swing',value_state:'observed',unit:'event_order',reference:'forward_swing',evidence_ids:ids,limitations:['Fixed synthetic observation, not force or torque']});
    if(task.sample.visible_objects.includes('feet'))metrics.push({metric_id:'support-continuity',definition:'Visible support continuity within the movement',value:'continuous',value_state:'observed',unit:'relative',reference:'action phase',evidence_ids:ids,limitations:['2D visibility proxy, not measured ground reaction force']});
    return {metrics,simulation:true};
  }
  private resource(task:FixtureTask,file:string,kind:string,mime:string,bytes:Buffer):Resource {
    const relative=`projects/${task.id}/v1/${file}`;const absolute=this.boundary.publicTask(relative,task.id,task.version);
    fs.mkdirSync(path.dirname(absolute),{recursive:true});const temporary=absolute+'.'+randomUUID()+'.tmp';
    try{fs.writeFileSync(temporary,bytes,{flag:'wx',mode:0o600});fs.renameSync(temporary,absolute);}catch(error){fs.rmSync(temporary,{force:true});throw error;}
    const value:Resource={resource_id:`resource-${randomUUID()}`,path:relative,mime,sha256:createHash('sha256').update(bytes).digest('hex'),size_bytes:bytes.length,kind,simulation:true};task.resources.set(value.resource_id,value);return value;
  }
  private asset(name:string):Buffer {
    if(!this.options.assetRoot)throw new Error('RESOURCE_UNAVAILABLE');const target=path.join(this.options.assetRoot,name);
    if(!fs.existsSync(target))throw new Error('RESOURCE_UNAVAILABLE');return fs.readFileSync(target);
  }
  private render(task:FixtureTask,p:JsonObject):unknown {
    this.authorize(task.input,'analysis');
    const ids=p.evidence_ids as string[];if(ids.some(id=>!this.evidence(task).some(e=>e.evidence_id===id)))throw new Error('EVIDENCE_UNKNOWN');
    const slow=p.mode==='slow_motion';const assetName=p.mode==='trajectory'?'trajectory.png':`${task.sample.fixture_id}.${slow?'mp4':'png'}`;
    const resource=this.resource(task,slow?'evidence/slow.mp4':`evidence/${String(p.mode)}.png`,slow?'evidence_clip':p.mode==='trajectory'?'trajectory':'evidence_frame',slow?'video/mp4':'image/png',this.asset(assetName));
    const evidence=this.evidence(task).map(e=>({...e,evidence_id:String(e.evidence_id)+'-'+String(p.mode),kind:slow?'video_clip':p.mode==='trajectory'?'trajectory':'video_frame',resource}));
    return {resources:[resource],evidence,simulation:true};
  }
  private target(task:FixtureTask,p:JsonObject):unknown {
    const resource=this.resource(task,'target/target.png','target_pose','image/png',this.asset('target.png'));
    return {kind:'target_pose',resource,features:p.features,limitations:['Generic fixed target skeleton; no player identity and no measurement evidence'],simulation:true};
  }
  private compare(task:FixtureTask,p:JsonObject):unknown {
    this.authorize(task.input,'retest');const other=this.get({task_id:p.other_task_id,version:p.other_version});
    this.authorize(other.input,'retest');if(!['complete','partial'].includes(other.state))throw new Error('COMPARISON_UNAVAILABLE');
    const differences:string[]=[];if(task.sample.device_scenario!==other.sample.device_scenario)differences.push('camera_scenario');if(task.input.user.target_stroke!==other.input.user.target_stroke)differences.push('stroke_task');if(task.sample.visible_objects.join(',')!==other.sample.visible_objects.join(','))differences.push('visibility');
    if(task.input.user.handedness!==other.input.user.handedness)differences.push('handedness');
    const changes=differences.length?[]:[{metric_id:'relative-order-change',definition:'Relative preparation order change in identical fixed fixtures',value:0,value_state:'observed',unit:'relative',reference:other.id,evidence_ids:[...this.evidence(task),...this.evidence(other)].map(e=>e.evidence_id),limitations:['Simulated zero change is not measured improvement']}];
    return {comparable:differences.length===0,differences,changes,simulation:true};
  }
  private export(task:FixtureTask,p:JsonObject):unknown {
    this.authorize(task.input,'diagnostics',p.consent as unknown as Consent);
    const selected=p.selected_resource_ids as string[];if(selected.some(id=>!task.resources.has(id)))throw new Error('RESOURCE_UNKNOWN');
    const safe=selected.map(id=>task.resources.get(id)!).filter(r=>r.kind!=='input_copy');
    const bytes=Buffer.from(JSON.stringify({task_id:task.id,version:task.version,state:task.state,selected:safe.map(r=>({resource_id:r.resource_id,kind:r.kind,sha256:r.sha256})),simulation:true}));
    const manifest=this.resource(task,'diagnostics/manifest.json','diagnostics','application/json',bytes);
    return {package_id:`package-${randomUUID()}`,manifest,resources:safe,redacted:true,simulation:true};
  }
  private remove(p:JsonObject):unknown {
    const selected=p.scope==='all_demo'?[...this.tasks.values()]:[this.get(p)];const items:{resource_id:string;status:string;code:string|null}[]=[];
    for(const task of selected){
      if(task.timer)clearTimeout(task.timer);
      if(this.options.fault==='delete_partial'){task.state='cancelled';items.push({resource_id:task.id,status:'failed',code:'SIMULATED_DELETE_FAILURE'});continue;}
      fs.rmSync(this.boundary.publicTask(`projects/${task.id}/v1`,task.id,task.version),{recursive:true,force:true});task.resources.clear();task.state='deleted';items.push({resource_id:task.id,status:'deleted',code:null});
    }
    return {operation_id:p.operation_id,items,complete:items.every(i=>i.status==='deleted'),external_copies_uncontrolled:true,runtime_uninstalled:false,simulation:true};
  }
}
