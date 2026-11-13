import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
const id=v=>typeof v==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(v);
export function requireCondition(value,code){if(!value)throw new Error(code);}
export function fingerprint(value){const sort=v=>Array.isArray(v)?v.map(sort):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b)).map(([k,x])=>[k,sort(x)])):v;return createHash('sha256').update(JSON.stringify(sort(value))).digest('hex');}
export function createPublicStore(context){
  requireCondition(context&&id(context.task_id)&&Number.isInteger(context.version)&&context.version>=1,'TASK_CONTEXT_INVALID');
  requireCondition(typeof context.simulation==='boolean','SIMULATION_FLAG_REQUIRED');
  const relative=`projects/${context.task_id}/v${context.version}`;requireCondition(context.workspace_ref===relative,'WORKSPACE_VERSION_MISMATCH');
  const root=fs.realpathSync(context.dataRoot),task=path.join(root,relative);
  function check(target){
    requireCondition(fs.existsSync(task)&&fs.statSync(task).isDirectory(),'PROJECT_DELETED_OR_UNAVAILABLE');
    let p=root;for(const segment of path.relative(root,target).split(path.sep)){p=path.join(p,segment);if(fs.existsSync(p))requireCondition(!fs.lstatSync(p).isSymbolicLink(),'SYMLINK_FORBIDDEN');}
    requireCondition(path.relative(root,target)!=='..'&&!path.relative(root,target).startsWith('../')&&fs.realpathSync(task)===task,'PUBLIC_PATH_FORBIDDEN');
  }
  const filename=name=>{requireCondition(/^[a-z][a-z0-9-]{0,63}\.json$/.test(name),'RECORD_NAME_INVALID');const target=path.join(task,'records',name);check(target);return target;};
  return {
    read(name,fallback=[]){const target=filename(name);return fs.existsSync(target)?JSON.parse(fs.readFileSync(target,'utf8')):structuredClone(fallback);},
    write(name,value){const target=filename(name);fs.mkdirSync(path.dirname(target),{recursive:true});check(target);const temp=target+'.'+randomUUID()+'.tmp';fs.writeFileSync(temp,JSON.stringify(value,null,2)+'\n',{flag:'wx',mode:0o600});check(target);fs.renameSync(temp,target);return path.relative(root,target).split(path.sep).join('/');}
  };
}
const safetyValues=['clear','fatigue','pain','injury','dizziness','worsening','rehab','uncertain'];
export function appendTrainingRecord(context,input){
  const keys=['record_id','drill_id','recorded_at','completion','actual_reps','duration_s','difficulty','feeling','body_feedback'];
  requireCondition(input&&Object.keys(input).length===keys.length&&keys.every(k=>Object.hasOwn(input,k)),'RECORD_FIELDS_INVALID');
  requireCondition(id(input.record_id)&&id(input.drill_id)&&/^DR-/.test(input.drill_id),'RECORD_ID_INVALID');
  const library=JSON.parse(fs.readFileSync(new URL('../references/drills.json',import.meta.url),'utf8'));
  requireCondition(library.drills.some(d=>d.id===input.drill_id),'DRILL_UNKNOWN');
  requireCondition(typeof input.recorded_at==='string'&&Number.isFinite(Date.parse(input.recorded_at)),'RECORD_TIME_INVALID');
  requireCondition(['done','partial','skipped'].includes(input.completion)&&safetyValues.includes(input.body_feedback),'RECORD_ENUM_INVALID');
  for(const key of ['actual_reps','duration_s'])requireCondition(input[key]===null||(Number.isFinite(input[key])&&input[key]>=0&&input[key]<=86400),'RECORD_DOSE_INVALID');
  requireCondition(input.actual_reps===null||Number.isInteger(input.actual_reps),'RECORD_DOSE_INVALID');
  for(const key of ['difficulty','feeling'])requireCondition(typeof input[key]==='string'&&input[key].length<=2048,'RECORD_TEXT_INVALID');
  const store=createPublicStore(context),rows=store.read('training.json'),hash=fingerprint(input),prior=rows.find(x=>x.record_id===input.record_id);
  if(prior){requireCondition(prior.input_digest===hash,'RECORD_ID_CONFLICT');return prior;}
  const safety=['pain','injury','dizziness','worsening'].includes(input.body_feedback)?'safety_stop':['rehab','uncertain'].includes(input.body_feedback)?'needs_review':'none';
  const row={...structuredClone(input),task_id:context.task_id,version:context.version,input_digest:hash,safety_action:safety,improvement_established:false,simulation:context.simulation===true};
  rows.push(row);store.write('training.json',rows);return row;
}
