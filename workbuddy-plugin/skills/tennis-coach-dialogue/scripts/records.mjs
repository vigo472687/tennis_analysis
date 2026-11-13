import {createPublicStore,requireCondition as require,fingerprint} from '../../tennis-training-plan/scripts/records.mjs';
const id=x=>typeof x==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(x);
function text(x){require(typeof x==='string'&&x.length<=4096,'COACH_TEXT_INVALID');return x.replace(/\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b/g,'[邮箱已隐藏]').replace(/(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)/g,'[电话已隐藏]').replace(/(?:sk-|Bearer\s+)[A-Za-z0-9_-]{8,}/gi,'[凭据已隐藏]');}
function list(x){require(Array.isArray(x)&&x.length<=20,'COACH_LIST_INVALID');return x.map(text);}
export function appendCoachRound(context,input){
  require(input&&id(input.round_id)&&/^coach-[A-Za-z0-9_-]{1,32}$/.test(input.coach_id)&&Number.isInteger(input.round)&&input.round>=1,'COACH_ROUND_INVALID');
  require(['user_summary','user_transcript','coach_material'].includes(input.origin),'COACH_ORIGIN_INVALID');
  require(input.identity_hidden_confirmed===true,'IDENTITY_REDACTION_REQUIRED');
  require(Array.isArray(input.evidence_ids)&&input.evidence_ids.length<=20&&input.evidence_ids.every(id),'COACH_EVIDENCE_INVALID');
  const content={round_id:input.round_id,coach_id:input.coach_id,round:input.round,origin:input.origin,question:text(input.question),reply:text(input.reply),goal:text(input.goal),conditions:text(input.conditions),evidence_ids:[...input.evidence_ids],agreements:list(input.agreements),unresolved:list(input.unresolved)};
  const store=createPublicStore(context),rows=store.read('coach-rounds.json'),hash=fingerprint(content),prior=rows.find(x=>x.round_id===input.round_id);
  if(prior){require(prior.input_digest===hash,'COACH_ROUND_CONFLICT');return prior;}
  require(input.round===rows.filter(x=>x.coach_id===input.coach_id).length+1,'COACH_ROUND_ORDER_INVALID');
  const row={...content,task_id:context.task_id,version:context.version,input_digest:hash,simulation:context.simulation};rows.push(row);store.write('coach-rounds.json',rows);return row;
}
export function savePracticeExperiment(context,input){
  require(input&&id(input.experiment_id)&&/^coach-[A-Za-z0-9_-]{1,32}$/.test(input.coach_id),'EXPERIMENT_ID_INVALID');
  require(input.safety==='clear'||input.safety==='fatigue','EXPERIMENT_SAFETY_BLOCKED');
  require(Array.isArray(input.variables)&&input.variables.length>=1&&input.variables.length<=2,'EXPERIMENT_VARIABLES_INVALID');
  require(Number.isInteger(input.days)&&input.days>=1&&input.days<=14,'EXPERIMENT_PERIOD_INVALID');
  const content={experiment_id:input.experiment_id,coach_id:input.coach_id,goal:text(input.goal),variables:input.variables.map(text),conditions:text(input.conditions),days:input.days,dose:text(input.dose),success:text(input.success),stop:text(input.stop),retest:text(input.retest),safety:input.safety};
  require(['goal','conditions','dose','success','stop','retest'].every(k=>content[k].trim().length),'EXPERIMENT_FIELDS_REQUIRED');
  const store=createPublicStore(context),rounds=store.read('coach-rounds.json');require(rounds.some(x=>x.coach_id===input.coach_id),'COACH_CONTEXT_MISSING');
  const rows=store.read('coach-experiments.json'),hash=fingerprint(content),prior=rows.find(x=>x.experiment_id===input.experiment_id);if(prior){require(prior.input_digest===hash,'EXPERIMENT_ID_CONFLICT');return prior;}
  const row={...content,task_id:context.task_id,version:context.version,input_digest:hash,dose_basis:'prototype_candidate',simulation:context.simulation};rows.push(row);store.write('coach-experiments.json',rows);return row;
}
export function evaluateCoachFit(context,input){
  const store=createPublicStore(context),experiments=store.read('coach-experiments.json'),experiment=experiments.find(x=>x.experiment_id===input.experiment_id);require(experiment,'EXPERIMENT_UNKNOWN');
  const rounds=store.read('coach-rounds.json').filter(x=>x.coach_id===experiment.coach_id);
  const sufficient=input.execution_sufficient===true&&input.conditions_comparable===true&&Number.isInteger(input.retest_count)&&input.retest_count>=2&&rounds.length>=2;
  const conclusion=!sufficient?'证据不足':input.goal_met===true?'适合当前目标':'继续观察';
  const result={experiment_id:experiment.experiment_id,coach_id:experiment.coach_id,conclusion,reason:!sufficient?'讨论、执行量或可比复测不足；不能归因教学问题':input.goal_met===true?'在本次目标与条件下观察到适配':'当前目标尚未确认达到，需继续讨论或调整条件',teaching_fault_established:false,simulation:context.simulation};
  const rows=store.read('coach-evaluations.json');rows.push(result);store.write('coach-evaluations.json',rows);return result;
}
