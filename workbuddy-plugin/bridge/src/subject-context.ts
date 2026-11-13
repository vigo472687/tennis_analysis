import fs from 'node:fs';
import path from 'node:path';
import {Phase1Error} from './phase1-backend.js';
import type {ContractRegistry} from './contracts.js';
export const SUBJECT_FILES=['pose-frames.jsonl','pose-frames-context.jsonl','action-selection.json','segment-states.json','motion-trajectories.json','motion-angles.jsonl','metrics.json'];
export interface SubjectCandidate {subject_id:string;frames:number;share:number;persistent:boolean;first_s:number;last_s:number;size_ratio:number;[key:string]:unknown;}
export interface SubjectGroup {subject_id:string;candidate:SubjectCandidate;metrics:any[];selection:any;unavailable_files?:string[];}
export class SubjectContext {
 constructor(private readonly configuredRoot:()=>string|null,private readonly registry?:ContractRegistry){}
 scope(workspace:string){if(!/^projects\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/v[1-9]\d*$/.test(workspace))throw new Phase1Error('WORKSPACE_REF_INVALID',400);const root=this.configuredRoot();if(!root)throw new Phase1Error('DATA_ROOT_REQUIRED',409);const real=fs.realpathSync(root),base=path.join(real,'public',workspace);this.exact(path.join(base,'records/task-config.json'));return {root:real,base};}
 exact(file:string){if(!fs.existsSync(file)||fs.realpathSync(file)!==file||!fs.statSync(file).isFile())throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);return file;}
 json(file:string){this.exact(file);if(fs.statSync(file).size>1024*1024)throw new Phase1Error('ARTIFACT_TOO_LARGE',413);try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);}}
 read(workspace:string){const {root,base}=this.scope(workspace),config=this.json(base+'/records/task-config.json'),file=base+'/records/subject-candidates.json';if(!fs.existsSync(file))return {workspace_ref:workspace,legacy:true,requested:config.subject_id??null,mode:'legacy',analyzed:[] as string[],candidates:[] as SubjectCandidate[],subject_stability:'unknown',note:'旧记录没有候选清单，可继续回看；需要选人时请重新上传处理。'};
 const v=this.json(file);if(!v||!['unconfirmed','single','all','invalid'].includes(v.mode)||!Array.isArray(v.candidates)||v.candidates.length>12||!Array.isArray(v.analyzed)||v.analyzed.length>6)throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);
 const seen=new Set<string>();for(const c of v.candidates){if(!/^track-\d+$/.test(c.subject_id)||seen.has(c.subject_id)||!Number.isInteger(c.frames)||c.frames<0||!Number.isFinite(c.share)||c.share<0||c.share>1||typeof c.persistent!=='boolean'||!Number.isFinite(c.first_s)||!Number.isFinite(c.last_s)||c.first_s<0||c.last_s<c.first_s||!Number.isFinite(c.size_ratio)||c.size_ratio<0||c.size_ratio>1)throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);seen.add(c.subject_id);}
 // null-mode backend may record analyzed:[null]; it is not a confirmed subject.
 if(v.mode==='unconfirmed'&&v.analyzed.length===1&&v.analyzed[0]===null)v.analyzed=[];
 if(new Set(v.analyzed).size!==v.analyzed.length||v.analyzed.some((id:unknown)=>typeof id!=='string'||!seen.has(id)))throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);
 if((config.subject_id??null)!==(v.requested??null))throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);
 if(v.mode==='all'&&(v.requested!=='all'||!v.analyzed.length)||v.mode==='single'&&(v.analyzed.length!==1||v.analyzed[0]!==v.requested)||v.mode==='unconfirmed'&&v.requested!==null)throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);
 if(fs.realpathSync(this.configuredRoot()!)!==root)throw new Phase1Error('RESOURCE_SCOPE_CHANGED',409);
 return {workspace_ref:workspace,legacy:false,requested:v.requested as string|null,mode:v.mode as string,analyzed:v.analyzed as string[],candidates:v.candidates as SubjectCandidate[],subject_stability:String(v.subject_stability),note:String(v.note||'')};}
 groups(workspace:string){const list=this.read(workspace);if(!['single','all'].includes(list.mode))return [];
 const {base}=this.scope(workspace);return list.analyzed.map(subject_id=>{const dir=base+'/records/subjects/'+subject_id;const data=this.json(dir+'/metrics.json'),selection=this.json(dir+'/action-selection.json');if(data.subject_id!==subject_id||data.simulation!==false||!Array.isArray(data.metrics)||data.metrics.length>100||selection.subject_id!==subject_id)throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);
 const unavailable_files:string[]=[];for(const name of SUBJECT_FILES){if(!fs.existsSync(dir+'/'+name)&&['motion-trajectories.json','motion-angles.jsonl'].includes(name)&&data.metrics.every((m:any)=>m.value_state!=='observed')){unavailable_files.push(name);continue;}this.exact(dir+'/'+name);}
 const ids=new Set();for(const metric of data.metrics){if(!metric||typeof metric.metric_id!=='string'||ids.has(metric.metric_id)||!['observed','insufficient_evidence','not_visible','not_applicable'].includes(metric.value_state)||(metric.value_state==='observed'?!Number.isFinite(metric.value):metric.value!==null))throw new Phase1Error('SUBJECT_ARTIFACT_INVALID',409);ids.add(metric.metric_id);this.registry?.validate('analysis','metric',metric);}
 return {subject_id,candidate:list.candidates.find(c=>c.subject_id===subject_id)!,metrics:data.metrics,selection,unavailable_files} as SubjectGroup;});}
 public(workspace:string){return {...this.read(workspace),subjects:this.groups(workspace)};}
 source(workspace:string){const {base,root}=this.scope(workspace),config=this.json(base+'/records/task-config.json'),ref=config.media_ref;if(typeof ref!=='string'||!/^ingest\/[A-Za-z0-9_.-]+\.(mp4|mov)$/i.test(ref))throw new Phase1Error('SUBJECT_SOURCE_UNAVAILABLE',409);this.exact(path.join(root,ref));return ref;}
}
