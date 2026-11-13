import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {Phase1Error} from './phase1-backend.js';
import {readActionSelection} from './action-selection.js';
import {readSegmentStates} from './segment-states.js';

const NAMES:Record<string,string>={segments:'records/segment-states.json',subjects:'records/subjects-summary.json',selection:'records/action-selection.json',pose:'records/pose-frames.jsonl',spec:'records/skeleton-spec.json',video:'media/standardized.mp4',config:'records/task-config.json'};
export class Phase3Media {
  constructor(private readonly configuredRoot:()=>string|null){}
  private publicRoot(){const root=this.configuredRoot();if(!root)throw new Phase1Error('DATA_ROOT_REQUIRED',409);const pub=path.join(fs.realpathSync(root),'public');if(!fs.existsSync(pub)||fs.realpathSync(pub)!==pub)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);return pub;}
  artifact(workspace:string,kind:string){
    if(!/^projects\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/v[1-9]\d*$/.test(workspace)||!NAMES[kind])throw new Phase1Error('WORKSPACE_REF_INVALID',400);
    const base=path.join(this.publicRoot(),workspace),config=path.join(base,NAMES.config!);
    if(!fs.existsSync(config)||fs.realpathSync(config)!==config)throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
    if(this.read(config).analysis_mode!=='real_video')throw new Phase1Error('REAL_VIDEO_WORKSPACE_REQUIRED',403);
    const file=path.join(base,NAMES[kind]!);if(!fs.existsSync(file))throw new Phase1Error('POSE_ARTIFACT_NOT_READY',404);
    if(fs.realpathSync(file)!==file)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);return file;
  }
  private read(file:string){if(fs.statSync(file).size>64*1024*1024)throw new Phase1Error('ARTIFACT_TOO_LARGE',413);try{return JSON.parse(fs.readFileSync(file,'utf8'));}catch{throw new Phase1Error('POSE_ARTIFACT_INVALID');}}
  selection(workspace:string){try{return readActionSelection(this.artifact(workspace,'selection'));}catch(e){if(e instanceof Phase1Error&&e.code==='POSE_ARTIFACT_NOT_READY')return null;throw e;}}
  segments(workspace:string){try{return readSegmentStates(this.artifact(workspace,'segments'),this.selection(workspace));}catch(e){if(e instanceof Phase1Error&&e.code==='POSE_ARTIFACT_NOT_READY')return null;throw e;}}
  availability(workspace:string){
    const segments=this.segments(workspace);
    let diagnostic:any=null;try{diagnostic=this.read(this.artifact(workspace,'subjects'));}catch(e){if(!(e instanceof Phase1Error&&e.code==='POSE_ARTIFACT_NOT_READY'))throw e;}
    if(diagnostic&&(!['single','multi','none'].includes(diagnostic.subject_stability)||diagnostic.requires_selection!==(diagnostic.subject_stability==='multi')||!Number.isSafeInteger(diagnostic.frames)||diagnostic.frames<0||!Number.isSafeInteger(diagnostic.lost_frames)||diagnostic.lost_frames<0||diagnostic.lost_frames>diagnostic.frames||!Array.isArray(diagnostic.notes)||!diagnostic.notes.every((n:unknown)=>typeof n==='string')))throw new Phase1Error('POSE_DIAGNOSTIC_INVALID',422);
    let ready=true;for(const kind of ['pose','spec','video'])try{this.artifact(workspace,kind);}catch(e){if(e instanceof Phase1Error&&e.code==='POSE_ARTIFACT_NOT_READY')ready=false;else throw e;}
    if(segments!==null){
      const posed=segments.filter(s=>s.status==='posed').length,deferred=segments.filter(s=>s.status==='deferred_multi_subject').length,skipped=segments.filter(s=>s.status==='skipped_no_subject').length;
      if(!posed){let empty=true;for(const kind of ['pose','video'])try{const file=this.artifact(workspace,kind);if(kind==='pose'&&fs.readFileSync(file,'utf8').trim())throw new Phase1Error('POSE_ARTIFACT_INVALID');}catch(e){if(e instanceof Phase1Error&&e.code==='POSE_ARTIFACT_NOT_READY')empty=false;else throw e;}ready=empty;}
      return {ready,code:ready?null:'POSE_ARTIFACT_NOT_READY',label:ready?posed?(deferred+skipped?'部分段姿态就绪':'逐段姿态就绪'):'逐段完成 · 无可用骨架':'逐段工件未齐',subject_stability:diagnostic?.subject_stability??null,requires_selection:deferred>0,notes:diagnostic?.notes??[],diagnostic_source:diagnostic?.source??null,segment_counts:{posed,deferred_multi_subject:deferred,skipped_no_subject:skipped},mode:'per_segment'};
    }
    const code=ready?null:diagnostic?.subject_stability==='multi'?'POSE_SUBJECT_SELECTION_REQUIRED':diagnostic?.subject_stability==='none'?'POSE_NO_STABLE_SUBJECT':'POSE_ARTIFACT_NOT_READY';
    const label=ready?'姿态就绪':code==='POSE_SUBJECT_SELECTION_REQUIRED'?'多人 · 姿态已跳过':code==='POSE_NO_STABLE_SUBJECT'?'无稳定主体 · 姿态已跳过':'无姿态工件';
    return {ready,code,label,subject_stability:diagnostic?.subject_stability??null,requires_selection:diagnostic?.requires_selection??null,notes:diagnostic?.notes??[],diagnostic_source:diagnostic?.source??null};
  }
  tasks(){const items:{workspace_ref:string;task_id:string;version:number;pose_ready:boolean;pose_status:string|null;pose_label:string;updated_at:string}[]=[];const projects=path.join(this.publicRoot(),'projects');if(!fs.existsSync(projects))return items;
    if(fs.realpathSync(projects)!==projects)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);
    for(const task of fs.readdirSync(projects,{withFileTypes:true})){if(!task.isDirectory()||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(task.name))continue;
      const base=path.join(projects,task.name);for(const v of fs.readdirSync(base,{withFileTypes:true})){if(!v.isDirectory()||!/^v[1-9]\d*$/.test(v.name))continue;const workspace='projects/'+task.name+'/'+v.name;
        try{const file=this.artifact(workspace,'config'),a=this.availability(workspace);
          items.push({workspace_ref:workspace,task_id:task.name,version:Number(v.name.slice(1)),pose_ready:a.ready,pose_status:a.code,pose_label:a.label,updated_at:fs.statSync(file).mtime.toISOString()});}catch{}
      }
    }return items.sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
  }
  result(workspace:string){
    const availability=this.availability(workspace);if(!availability.ready&&availability.code!=='POSE_ARTIFACT_NOT_READY')throw new Phase1Error(availability.code!,409);
    const selection=this.selection(workspace);
    const segments=this.segments(workspace),emptySegments=segments!==null&&!segments.some(s=>s.status==='posed');
    const spec=emptySegments?null:this.read(this.artifact(workspace,'spec'));const fail=()=>{throw new Phase1Error('POSE_ARTIFACT_INVALID');};
    if(spec){
    if(!Array.isArray(spec.keypoints)||spec.keypoints.length!==133||!Array.isArray(spec.skeleton)||spec.skeleton.length!==65)fail();
    const names=new Set<string>();for(const [i,k] of spec.keypoints.entries()){if(k.index!==i||typeof k.name!=='string'||names.has(k.name)||!this.color(k.color))fail();names.add(k.name);}
    for(const [i,l] of spec.skeleton.entries())if(l.index!==i||!names.has(l.from)||!names.has(l.to)||!this.color(l.color))fail();
    }
    const file=this.artifact(workspace,'pose');if(fs.statSync(file).size>64*1024*1024)throw new Phase1Error('ARTIFACT_TOO_LARGE',413);
    let frames:any[];try{frames=fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean).map(l=>JSON.parse(l));}catch{return fail();}
    const expectedFrames=segments!==null?segments.filter(s=>s.status==='posed').reduce((n,s)=>n+s.frames,0):selection?.selected_frames;
    if((!frames.length&&expectedFrames!==0)||frames.length>20000||(expectedFrames!==undefined&&frames.length!==expectedFrames))fail();let prev=-1,previousFrame=-1;const failures:Record<string,number>={};let withPose=0;
    for(const f of frames){
      if(!Number.isSafeInteger(f.frame)||f.frame<0||f.frame<=previousFrame||!Number.isFinite(f.pts_s)||f.pts_s<0||f.pts_s<=prev)fail();prev=f.pts_s;previousFrame=f.frame;
      if(selection?.details_available&&!selection.selected_segments!.some(s=>f.frame>=s.start_frame&&f.frame<=s.end_frame))fail();
      if(segments!==null&&!segments.some(s=>s.status==='posed'&&f.frame>=s.segment[0]&&f.frame<=s.segment[1]))fail();
      if(f.failure!==null){if(typeof f.failure!=='string'||!f.failure||f.keypoints!==null||f.valid!==null)fail();failures[f.failure]=(failures[f.failure]||0)+1;continue;}
      if(!spec||f.subject_overlap!==true||!Array.isArray(f.subject_box)||f.subject_box.length!==4||!f.subject_box.every(Number.isFinite)||!Array.isArray(f.keypoints)||f.keypoints.length!==spec.keypoints.length||!Array.isArray(f.valid)||f.valid.length!==spec.keypoints.length)fail();
      for(const [n,k] of f.keypoints.entries())if(!Array.isArray(k)||k.length!==3||!k.every(Number.isFinite)||typeof f.valid[n]!=='boolean')fail();withPose++;
    }
    const video=this.artifact(workspace,'video');let stream:any;
    try{stream=JSON.parse(execFileSync('ffprobe',['-v','error','-select_streams','v:0','-show_entries','stream=width,height,r_frame_rate,duration','-of','json',video],{encoding:'utf8',timeout:15000})).streams[0];}catch{throw new Phase1Error('POSE_VIDEO_INVALID');}
    if(!Number.isInteger(stream?.width)||!Number.isInteger(stream?.height)||stream.width<1||stream.height<1)throw new Phase1Error('POSE_VIDEO_INVALID');
    const ratio=String(stream.r_frame_rate).split('/').map(Number),fps=ratio[0]!/ratio[1]!;if(!Number.isFinite(fps)||fps<=0)throw new Phase1Error('POSE_VIDEO_INVALID');
    if(selection){for(const s of selection.selected_segments||[])if(Math.abs(s.start_s-s.start_frame/fps)>.005||Math.abs(s.end_s-s.end_frame/fps)>.005)fail();}
    if(segments!==null)for(const s of segments)if(s.status==='posed'&&frames.filter(f=>f.frame>=s.segment[0]&&f.frame<=s.segment[1]&&f.failure===null).length!==s.pose_frames)fail();
    for(const f of frames)if(Math.abs(f.pts_s-f.frame/fps)>.005)fail();
    const q=encodeURIComponent(workspace),url=(kind:string)=>'/phase3/artifact?workspace_ref='+q+'&kind='+kind;
    const config=this.read(this.artifact(workspace,'config'));
    return {workspace_ref:workspace,validation_label:typeof config.validation_source==='string'?config.validation_source:null,selection,segments,spec,availability,frames,video:{width:stream.width,height:stream.height,fps,duration_s:Number(stream.duration),url:url('video'),time_basis:'standardized_video_pts'},summary:{frames:frames.length,with_pose:withPose,failures,keypoints:spec?.keypoints.length??0,links:spec?.skeleton.length??0,first_pts:frames[0]?.pts_s??null,last_pts:frames.at(-1)?.pts_s??null},downloads:{pose:url('pose'),spec:spec?url('spec'):null,segments:segments!==null?url('segments'):null,selection:selection?url('selection'):null}};
  }
  private color(x:any){return Array.isArray(x)&&x.length===3&&x.every(v=>Number.isInteger(v)&&v>=0&&v<=255);}
}
