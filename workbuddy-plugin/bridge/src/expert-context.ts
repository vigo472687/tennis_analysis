import type {ContractRegistry} from './contracts.js';
import {SubjectContext,SUBJECT_FILES,type SubjectGroup} from './subject-context.js';
import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {Phase3Media} from './phase3-media.js';
import type {Phase4Service} from './phase4-service.js';
import {ExpertError} from './expert-policy.js';

export const expertHash=(value:Buffer|string)=>createHash('sha256').update(value).digest('hex');
export interface ExpertFrame {subject_id?:string;evidence_id:string;kind:'single_frame';original_s:number;segment_original_end_s:number;sha256:string;size_bytes:number;path:string;url:string;}
export interface VideoContext {
  root:string;workspace_ref:string;task_id:string;version:number;fingerprint:string;
  files:{relative:string;sha256:string}[];frames:ExpertFrame[];
  summary:{subject_mode?:string;requested_subject?:string|null;subjects?:SubjectGroup[];task_id:string;version:number;media:{sha256:string;duration_s:number;width:number;height:number};time_mapping_valid:boolean;subject_confirmed:boolean;quality:unknown;metrics:any[];timing:unknown;strokes:unknown;selection:unknown;limitations:string[]};
}
export class ExpertContext {
  constructor(private readonly configuredRoot:()=>string|null,private readonly phase4:Pick<Phase4Service,'query'|'render'>,private readonly registry?:ContractRegistry){}
  root(){const root=this.configuredRoot();if(!root)throw new ExpertError('DATA_ROOT_REQUIRED',409);return fs.realpathSync(root);}
  private exact(root:string,relative:string){
    if(!relative||path.isAbsolute(relative)||relative.includes('\\')||relative.split('/').some(x=>x==='..'||x==='.')||relative.includes('\0'))throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    const file=path.join(root,relative);if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()||fs.realpathSync(file)!==file)throw new ExpertError('EXPERT_CONTEXT_ARTIFACT_MISSING',409);return file;
  }
  private digest(file:string){const hash=createHash('sha256'),buffer=Buffer.alloc(1024*1024),fd=fs.openSync(file,'r');try{let count;while((count=fs.readSync(fd,buffer,0,buffer.length,null)))hash.update(buffer.subarray(0,count));return hash.digest('hex');}finally{fs.closeSync(fd);}}
  private json(file:string){if(fs.statSync(file).size>1024*1024)throw new ExpertError('EXPERT_CONTEXT_TOO_LARGE',413);try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{throw new ExpertError('EXPERT_CONTEXT_INVALID',409);}}
  async prepare(workspace:string):Promise<VideoContext>{
    const root=this.root(),pose=new Phase3Media(()=>root);pose.artifact(workspace,'config');
    const [,task_id,v]=workspace.split('/'),version=Number(v!.slice(1)),relative='public/'+workspace+'/';
    const read=(name:string)=>this.json(this.exact(root,relative+'records/'+name));
    const config=read('task-config.json');if(config.analysis_mode!=='real_video')throw new ExpertError('EXPERT_REAL_CONTEXT_REQUIRED',403);
    if(fs.existsSync(path.join(root,relative,'records/cancellation.json')))throw new ExpertError('EXPERT_CANCELLED_TASK',409);
    const notes=read('analysis-notes.json'),asset=read('media-asset.json'),subjects=read('subjects-summary.json');
    if(!['single','multi','none'].includes(subjects.subject_stability))throw new ExpertError('EXPERT_CONTEXT_INVALID',409);
    if(config.subject_id!==null&&config.subject_id!==undefined&&config.subject_id!=='all'&&(typeof config.subject_id!=='string'||!/^track-\d+$/.test(config.subject_id)))throw new ExpertError('EXPERT_SUBJECT_INVALID',409);
    if(subjects.subject_stability==='multi'&&!config.subject_id)throw new ExpertError('EXPERT_SUBJECT_UNCONFIRMED',409);
    if(asset.time_mapping_valid!==true||!Array.isArray(asset.time_mapping)||asset.time_mapping.length<2)throw new ExpertError('EXPERT_TIME_MAPPING_INVALID',409);
    let lastN=-1,lastO=-1;for(const point of asset.time_mapping){if(!Number.isFinite(point.normalized_s)||!Number.isFinite(point.original_s)||point.normalized_s<=lastN||point.original_s<=lastO)throw new ExpertError('EXPERT_TIME_MAPPING_INVALID',409);lastN=point.normalized_s;lastO=point.original_s;}
    const subjectReader=new SubjectContext(()=>root,this.registry),candidateList=subjectReader.read(workspace);if(candidateList.mode==='invalid')throw new ExpertError('EXPERT_SUBJECT_INVALID',409);const groups=subjectReader.groups(workspace);if(config.subject_id==='all'&&(candidateList.mode!=='all'||!groups.length))throw new ExpertError('EXPERT_SUBJECT_RECORDS_MISSING',409);if(config.subject_id&&/^track-/.test(config.subject_id)&&!candidateList.legacy&&candidateList.mode!=='single')throw new ExpertError('EXPERT_SUBJECT_INVALID',409);
    const query=await this.phase4.query(workspace);if(this.root()!==root)throw new ExpertError('RESOURCE_SCOPE_CHANGED',409);
    const results=query.results as Record<string,any>;
    for(const key of ['quality','metrics','timing','strokes'])if(results[key]?.simulation!==false)throw new ExpertError('EXPERT_REAL_CONTEXT_REQUIRED',403);
    if(results.quality.time_mapping_valid!==true||!Array.isArray(results.metrics.metrics))throw new ExpertError('EXPERT_TIME_MAPPING_INVALID',409);
    const selection=pose.selection(workspace),availability=pose.availability(workspace);
    if(!availability.ready)throw new ExpertError('EXPERT_CONTEXT_ARTIFACT_MISSING',409);
    const names=['records/task-config.json','records/media-asset.json','records/subjects-summary.json','records/analysis-notes.json','records/pose-frames.jsonl','records/action-selection.json','media/standardized.mp4'];
    for(const optional of ['records/segment-states.json','records/pose-frames-context.jsonl'])if(fs.existsSync(path.join(root,relative,optional)))names.push(optional);
    if(!candidateList.legacy)names.push('records/subject-candidates.json');for(const group of groups)for(const name of SUBJECT_FILES)if(!(group.unavailable_files||[]).includes(name))names.push('records/subjects/'+group.subject_id+'/'+name);
    const files=names.map(name=>({relative:relative+name,sha256:this.digest(this.exact(root,relative+name))}));
    const frames:ExpertFrame[]=[];
    if((selection?.selected_count??0)>0&&subjects.subject_stability!=='none'){
      const rendered=await this.phase4.render(workspace,'frame');if(this.root()!==root)throw new ExpertError('RESOURCE_SCOPE_CHANGED',409);
      for(const item of rendered.evidence as any[]){
        const resource=(rendered.resources as any[]).find(r=>r.resource_id===item.resource?.resource_id);
        if(!resource||item.simulation!==false||resource.simulation!==false||resource.mime!=='image/png'||resource.kind!=='evidence_frame'||item.asset_id!==asset.media_id||!item.evidence_id.startsWith('ev-'+task_id+'-v'+version+'-seg-')||resource.path!==workspace+'/evidence/'+item.evidence_id+'.png')throw new ExpertError('EXPERT_EVIDENCE_MISMATCH',409);
        if(!Number.isFinite(item.original_start_s)||item.original_start_s<0||!Number.isFinite(item.original_end_s)||item.original_end_s<item.original_start_s||item.original_end_s>results.quality.media.duration_s+.2)throw new ExpertError('EXPERT_EVIDENCE_MISMATCH',409);
        const file=this.exact(root,'public/'+resource.path);if(fs.statSync(file).size!==resource.size_bytes||this.digest(file)!==resource.sha256)throw new ExpertError('EXPERT_EVIDENCE_MISMATCH',409);
        frames.push({...((groups[0])?{subject_id:groups[0].subject_id}:{}),evidence_id:item.evidence_id,kind:'single_frame',original_s:item.original_start_s,segment_original_end_s:item.original_end_s,sha256:resource.sha256,size_bytes:resource.size_bytes,path:resource.path,url:resource.url});
        files.push({relative:'public/'+resource.path,sha256:resource.sha256});
      }
    }
    const probe=asset.probe;if(!probe||!Number.isFinite(probe.duration_s)||!Number.isInteger(probe.width)||!Number.isInteger(probe.height))throw new ExpertError('EXPERT_CONTEXT_INVALID',409);
    const all=config.subject_id==='all';
    const summary={subject_mode:candidateList.mode,requested_subject:config.subject_id??null,subjects:groups,task_id:task_id!,version,media:{sha256:files.find(f=>f.relative.endsWith('/media/standardized.mp4'))!.sha256,duration_s:probe.duration_s,width:probe.width,height:probe.height},time_mapping_valid:true,subject_confirmed:subjects.subject_stability==='single'||Boolean(config.subject_id),quality:all?{scope:'primary_subject_only',subject_id:groups[0]?.subject_id,value:results.quality}:results.quality,metrics:all?[]:(groups[0]?.metrics||results.metrics.metrics),timing:all?null:results.timing,strokes:all?null:results.strokes,selection:all?null:selection,limitations:[...(all?['必须按subjects逐人解释，禁止混合指标、跨人比较或整场比赛动作评价。根扁平集和截图仅对应首位主体；每人的动作段以subjects中selection为准。用户背景尚未映射到具体track，不能当作全体人员的事实。']:[]),'只有本片段二维身体代理指标，不能判断真实发力、拍速、球路或职业水平。','关键画面kind=single_frame，仅在original_s处取一帧；segment_original_end_s只是所属片段终点，不表示看过这一区间视频。','未选中的帧可能来自闲置、间隔或数量上限，不能仅凭计数归因为数量上限。','后端历史任务中的持拍手、球龄、目标等测试默认值不作为用户事实。',...Object.values(notes).flat().filter((x):x is string=>typeof x==='string')]};
    const fingerprint=expertHash(JSON.stringify({task_id,version,files,summary}));const result={root,workspace_ref:workspace,task_id:task_id!,version,fingerprint,files,frames,summary};this.verify(result);return result;
  }
  verify(context:VideoContext){
    if(this.root()!==context.root)throw new ExpertError('RESOURCE_SCOPE_CHANGED',409);
    if(!/^projects\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/v[1-9]\d*$/.test(context.workspace_ref)||context.files.some(f=>!f.relative.startsWith('public/'+context.workspace_ref+'/')))throw new ExpertError('EXPERT_CONTEXT_PATH_DENIED',403);
    if(fs.existsSync(path.join(context.root,'public',context.workspace_ref,'records/cancellation.json')))throw new ExpertError('EXPERT_CANCELLED_TASK',409);
    const prefix='public/'+context.workspace_ref+'/records/';const candidatesFile=prefix+'subject-candidates.json';if(fs.existsSync(path.join(context.root,candidatesFile))!==context.files.some(f=>f.relative===candidatesFile))throw new ExpertError('EXPERT_CONTEXT_CHANGED',409);for(const group of context.summary?.subjects||[])for(const name of SUBJECT_FILES)if(context.files.some(f=>f.relative===prefix+'subjects/'+group.subject_id+'/'+name)!==fs.existsSync(path.join(context.root,prefix+'subjects/'+group.subject_id+'/'+name)))throw new ExpertError('EXPERT_CONTEXT_CHANGED',409);
    for(const item of context.files)if(this.digest(this.exact(context.root,item.relative))!==item.sha256)throw new ExpertError('EXPERT_CONTEXT_CHANGED',409);
  }
  select(context:VideoContext,ids:unknown):ExpertFrame[]{
    if(!Array.isArray(ids)||ids.length>3||new Set(ids).size!==ids.length||ids.some(id=>typeof id!=='string'||!context.frames.some(f=>f.evidence_id===id)))throw new ExpertError('EXPERT_FRAME_SELECTION_INVALID',400);
    const frames=context.frames.filter(f=>ids.includes(f.evidence_id));if(frames.reduce((n,f)=>n+f.size_bytes,0)>12*1024*1024)throw new ExpertError('EXPERT_FRAME_SELECTION_TOO_LARGE',413);return frames;
  }
  images(context:VideoContext,ids:string[]){this.verify(context);return this.select(context,ids).map(frame=>({type:'image_url' as const,image_url:{url:'data:image/png;base64,'+fs.readFileSync(this.exact(context.root,'public/'+frame.path)).toString('base64')}}));}
  public(context:VideoContext){return {workspace_ref:context.workspace_ref,task_id:context.task_id,version:context.version,fingerprint:context.fingerprint,summary:context.summary,frames:context.frames.map(({path,...frame})=>frame)};}
}
