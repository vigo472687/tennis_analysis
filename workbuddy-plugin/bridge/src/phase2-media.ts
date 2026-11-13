import fs from 'node:fs';
import path from 'node:path';
import {Readable,Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {randomUUID,createHash} from 'node:crypto';
import {Phase1Error} from './phase1-backend.js';
import {readActionSelection} from './action-selection.js';

export const MAX_UPLOAD=512*1024*1024;
export class Phase2Media {
  constructor(private readonly repoRoot:string,private readonly configuredRoot:()=>string|null){}
  private root(){const root=this.configuredRoot();if(!root)throw new Phase1Error('DATA_ROOT_REQUIRED',409);return fs.realpathSync(root);}
  async upload(input:Readable,filename:string){
    if(!filename||filename!==path.basename(filename)||/[\\/\x00-\x1f]/.test(filename)||filename.length>150)throw new Phase1Error('UPLOAD_NAME_INVALID',400);
    if(!/\.(mp4|mov)$/i.test(filename))throw new Phase1Error('UPLOAD_TYPE_INVALID',400);
    const root=this.root(),dir=path.join(root,'ingest');await fs.promises.mkdir(dir,{recursive:true});
    if(fs.lstatSync(dir).isSymbolicLink()||fs.realpathSync(dir)!==dir)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);
    // Internal references must satisfy the backend ASCII path contract; UI keeps filename.
    const storedName=filename.replace(/[^A-Za-z0-9_.-]/g,'_');
    const name=randomUUID().slice(0,8)+'-'+storedName,file=path.join(dir,name),temporary=file+'.part';let size=0;const hash=createHash('sha256');
    const count=new Transform({transform(chunk:Buffer,_encoding,done){size+=chunk.length;if(size>MAX_UPLOAD){done(new Phase1Error('UPLOAD_TOO_LARGE',413));return;}hash.update(chunk);done(null,chunk);}});
    try{
      await pipeline(input,count,fs.createWriteStream(temporary,{flags:'wx'}));
      if(!size)throw new Phase1Error('UPLOAD_EMPTY',400);
      await fs.promises.rename(temporary,file);
      return {filename,media_ref:'ingest/'+name,size_bytes:size,sha256:hash.digest('hex')};
    }finally{await fs.promises.rm(temporary,{force:true});}
  }
  approved(){return this.upload(fs.createReadStream(path.join(this.repoRoot,'005 后端重构/isolation/sample/approved-sample.mp4')),'approved-sample.mp4');}
  artifact(workspace:string,kind:string){
    if(!/^projects\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/v[1-9]\d*$/.test(workspace))throw new Phase1Error('WORKSPACE_REF_INVALID',400);
    const names:Record<string,string>={selection:'records/action-selection.json',video:'subjects/subjects-annotated.mp4',frames:'records/subjects-frames.jsonl',config:'records/task-config.json'};
    if(!names[kind])throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
    const pub=path.join(this.root(),'public');
    if(!fs.existsSync(pub)||fs.lstatSync(pub).isSymbolicLink()||fs.realpathSync(pub)!==pub)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);
    const file=path.join(pub,workspace,names[kind]);
    if(!fs.existsSync(file))throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
    if(fs.realpathSync(file)!==file)throw new Phase1Error('MEDIA_REF_OUTSIDE_ROOT',403);
    const configFile=path.join(pub,workspace,names.config!);
    if(!fs.existsSync(configFile)||fs.realpathSync(configFile)!==configFile)throw new Phase1Error('ARTIFACT_NOT_FOUND',404);
    const config=JSON.parse(fs.readFileSync(configFile,'utf8'));
    if(config.analysis_mode!=='real_video')throw new Phase1Error('REAL_VIDEO_WORKSPACE_REQUIRED',403);
    return file;
  }
  result(workspace:string){
    let selection=null;try{selection=readActionSelection(this.artifact(workspace,'selection'));}catch(e){if(!(e instanceof Phase1Error&&e.code==='ARTIFACT_NOT_FOUND'))throw e;}
    const file=this.artifact(workspace,'frames');if(fs.statSync(file).size>32*1024*1024)throw new Phase1Error('ARTIFACT_TOO_LARGE',413);
    const lines=fs.readFileSync(file,'utf8').trim().split('\n').filter(Boolean);
    if(lines.length>20000)throw new Phase1Error('ARTIFACT_TOO_LARGE',413);
    const frames=lines.map(line=>{const x=JSON.parse(line);if(!Number.isInteger(x.frame)||!Number.isFinite(x.pts_s)||typeof x.lost!=='boolean'||!Array.isArray(x.boxes)||(x.track_id!==null&&!Number.isInteger(x.track_id)))throw new Phase1Error('ARTIFACT_INVALID',422);return x;});
    const lost=frames.filter(x=>x.lost).length,detected=frames.filter(x=>x.boxes.length>0).length;
    const trackIds=[...new Set(frames.map(x=>x.track_id).filter(x=>x!==null))];
    const query=encodeURIComponent(workspace);this.artifact(workspace,'video');
    return {workspace_ref:workspace,selection,selection_url:selection?'/phase2/artifact?workspace_ref='+query+'&kind=selection':null,frames,summary:{frames:frames.length,detected_frames:detected,lost_frames:lost,retention:frames.length?(frames.length-lost)/frames.length:null,track_ids:trackIds,last_pts_s:frames.at(-1)?.pts_s??null},video_url:'/phase2/artifact?workspace_ref='+query+'&kind=video',frames_url:'/phase2/artifact?workspace_ref='+query+'&kind=frames'};
  }
}
