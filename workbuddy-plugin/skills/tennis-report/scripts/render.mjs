import fs from 'node:fs';import path from 'node:path';import {createHash} from 'node:crypto';import {fileURLToPath,pathToFileURL} from 'node:url';
import Ajv from '../../../bridge/node_modules/ajv/dist/ajv.js';
import {createPublicStore,requireCondition as require} from '../../tennis-training-plan/scripts/records.mjs';
export const escapeHtml=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fail=(v,code)=>require(v,code);
function pluginRoot(){const here=path.dirname(fileURLToPath(import.meta.url));return path.resolve(here,path.basename(here)==='bin'?'..':'../../..');}
export function validateReport(report,context,options={}){
  const plugin=options.pluginRoot||pluginRoot(),ajv=new Ajv({strict:false,allErrors:true});
  for(const file of fs.readdirSync(path.join(plugin,'contracts/v1')))if(file.endsWith('.schema.json'))ajv.addSchema(JSON.parse(fs.readFileSync(path.join(plugin,'contracts/v1',file),'utf8')));
  fail(ajv.getSchema('https://tennis.local/contracts/v1/report-input.schema.json#/definitions/report')(report),'REPORT_SCHEMA_INVALID');
  fail(report.task_id===context.task_id&&report.version===context.version&&report.simulation===context.simulation,'REPORT_CONTEXT_MISMATCH');
  createPublicStore(context).read('report-record.json',null);const root=fs.realpathSync(context.dataRoot),workspace=path.join(root,context.workspace_ref);
  const resource=r=>{
    // workspace_ref is the contract's public root, not an assumed subdirectory.
    fail(r.simulation===report.simulation&&r.path.startsWith(context.workspace_ref+'/'),'REPORT_RESOURCE_SCOPE_INVALID');
    const target=path.join(root,r.path);let p=root;for(const segment of path.relative(root,target).split(path.sep)){p=path.join(p,segment);if(fs.existsSync(p))fail(!fs.lstatSync(p).isSymbolicLink(),'REPORT_RESOURCE_SYMLINK');}
    fail(fs.existsSync(target)&&fs.statSync(target).isFile(),'REPORT_RESOURCE_UNAVAILABLE');
    fail(fs.realpathSync(target).startsWith(workspace+path.sep),'REPORT_RESOURCE_SCOPE_INVALID');
    const stat=fs.statSync(target);fail(stat.size===r.size_bytes&&stat.size<=64*1024*1024,'REPORT_RESOURCE_SIZE_INVALID');
    fail(createHash('sha256').update(fs.readFileSync(target)).digest('hex')===r.sha256,'REPORT_RESOURCE_HASH_INVALID');
    fail(['image/png','image/jpeg','video/mp4'].includes(r.mime),'REPORT_RESOURCE_MIME_INVALID');
  };
  const evidence=new Map(),sources=new Map();
  for(const e of report.evidence){fail(!evidence.has(e.evidence_id),'REPORT_DUPLICATE_ID');fail(e.simulation===report.simulation,'REPORT_SIMULATION_MISMATCH');fail(e.original_start_s<=e.original_end_s&&Number.isFinite(context.media_duration_s)&&e.original_end_s<=context.media_duration_s,'REPORT_EVIDENCE_TIME_INVALID');fail(e.subject_id===context.subject_id,'REPORT_SUBJECT_MISMATCH');if(e.resource)resource(e.resource);evidence.set(e.evidence_id,e);}
  for(const s of report.sources){fail(!sources.has(s.source_id),'REPORT_DUPLICATE_ID');let url;try{url=new URL(s.url);}catch{}fail(url?.protocol==='https:'&&!url.username&&!url.password,'REPORT_SOURCE_URL_INVALID');sources.set(s.source_id,s);}
  const trainingRoot=path.join(plugin,'skills/tennis-training-plan/references');
  const ground=JSON.parse(fs.readFileSync(path.join(trainingRoot,'groundstrokes.json'),'utf8')),foot=JSON.parse(fs.readFileSync(path.join(trainingRoot,'footwork.json'),'utf8')),sv=JSON.parse(fs.readFileSync(path.join(trainingRoot,'serve-volley.json'),'utf8'));
  const rules=new Map([...ground.observation_rules.map(r=>[r.id,r.requires]),[foot.rule_id,['body','feet','ball','racket']],...sv.strokes.map(r=>[r.rule_id,r.requires])]);
  const sourceMap=JSON.parse(fs.readFileSync(path.join(plugin,'skills/tennis-evidence-reasoning/references/source-map.json'),'utf8')).rules;
  for(const p of report.problems){
    fail(p.source_ids.length>0&&p.source_ids.every(id=>sources.has(id)),'REPORT_SOURCE_MISSING');fail(p.evidence_ids.every(id=>evidence.has(id)),'REPORT_EVIDENCE_MISSING');fail(p.confidence!=='insufficient_evidence','REPORT_ISSUE_INSUFFICIENT');
    if(report.primary_route==='technical_diagnosis'){
      const applied=context.problem_rules?.[p.problem_id];fail(Array.isArray(applied)&&applied.length>0,'REPORT_RULE_MAPPING_REQUIRED');
      for(const rule of applied){fail(rules.has(rule),'REPORT_RULE_UNKNOWN');fail(p.source_ids.some(id=>sourceMap.find(r=>r.rule_id===rule)?.source_ids.includes(id)),'REPORT_RULE_SOURCE_MISMATCH');fail(p.evidence_ids.some(id=>{const e=evidence.get(id);return !!e.resource&&rules.get(rule).every(object=>e.visible_objects.includes(object));}),'REPORT_ISSUE_NOT_VISIBLE');}
    }
  }
  for(const m of report.metrics){fail(m.evidence_ids.every(id=>evidence.has(id)),'REPORT_EVIDENCE_MISSING');if(m.value_state!=='observed')fail(m.value===null,'REPORT_MISSING_VALUE_NOT_NULL');else fail(m.value!==null&&m.evidence_ids.length>0,'REPORT_OBSERVATION_MISSING');}
  const requirements=JSON.parse(fs.readFileSync(path.join(plugin,'fixtures/expected/actions.json'),'utf8')).event_requires;
  for(const event of report.timing){fail(event.evidence_ids.every(id=>evidence.has(id)),'REPORT_EVIDENCE_MISSING');if(event.value_state!=='observed')fail(event.original_s===null,'REPORT_MISSING_TIME_NOT_NULL');else{fail(event.original_s!==null&&event.original_s<=context.media_duration_s,'REPORT_EVENT_TIME_INVALID');const objects=requirements[event.kind];fail(objects.every(x=>event.requires.includes(x))&&event.evidence_ids.some(id=>objects.every(x=>evidence.get(id).visible_objects.includes(x))),'REPORT_EVENT_NOT_VISIBLE');}}
  if(report.target){fail(report.target.simulation===report.simulation,'REPORT_SIMULATION_MISMATCH');resource(report.target.resource);fail(!report.evidence.some(e=>e.resource?.resource_id===report.target.resource.resource_id),'REPORT_TARGET_IS_EVIDENCE');}
  if(['pain','injury','dizziness','worsening'].includes(report.safety))fail(report.state==='safety_stop','REPORT_SAFETY_STATE_INVALID');
  if(['unsupported','reshoot_required','failed','cancelled'].includes(report.state))fail(report.problems.length===0&&report.training===null,'REPORT_NONCOMPLETE_CONTENT_INVALID');
  return {plugin,root,workspace};
}
export function renderReport(report,context,options={}){
  const {plugin,root,workspace}=validateReport(report,context,options),templateRoot=options.templateRoot||path.join(plugin,'skills/tennis-report/templates');
  const esc=escapeHtml,items=xs=>'<ul>'+xs.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>',link=(id,label)=>'<a href="#'+esc(id)+'">'+esc(label)+'</a>';
  const stateLabels={draft:'草稿',complete:'分析完成',partial:'部分完成',reshoot_required:'需要重拍',unsupported:'不支持此输入',safety_stop:'安全停止',failed:'生成失败',cancelled:'已取消'};
  const quick=report.problems.length?`<div class="quick-read"><p>先关注：${report.problems.map(p=>link('priorities',p.statement)).join('；')}</p><p>本周练习：${report.training?link('practice',report.training.drills[0].goal):'本次不安排个体负荷，先补证据或确认安全。'}</p></div>`:'';
  const summary=`<p class="eyebrow">${report.simulation?'固定样本模拟 · 非真实动作识别':'本地报告'}</p><h1>本次网球练习观察</h1><p class="state">${esc(stateLabels[report.state])}</p>${quick}<details class="summary-detail"><summary>本次说明与任务信息</summary><p>${esc(report.summary)}</p><p class="muted">任务 ${esc(report.task_id)} · 版本 ${report.version} · AI辅助，未由合作教练专业签署</p></details>`;
  const confidenceLabels={high:'充分',medium:'中等',low:'有限',insufficient_evidence:'证据不足'};
  const valueLabels={observed:'可见观察',unknown:'无法判断',not_provided:'未提供',not_visible:'不可见',not_applicable:'不适用',insufficient_evidence:'证据不足'};
  const problems=report.problems.map((p,i)=>`<article class="problem"><h3>${i+1}. ${esc(p.statement)}</h3><p>${esc(p.impact)}</p><p>证据充分程度：${esc(confidenceLabels[p.confidence])}；优先原因：${esc(p.priority_reason)}</p><p>${p.evidence_ids.map(id=>link('ev-'+id,'证据 '+id)).join(' · ')} ｜ ${p.source_ids.map(id=>link('src-'+id,'来源 '+id)).join(' · ')}</p>${items([...p.limitations,...p.alternative_explanations.map(x=>'替代解释：'+x)])}</article>`).join('')||'<p>本次没有可发布的个人技术问题，请查看状态与限制。</p>';
  const training=report.training?`<p>${report.training.weeks}周计划 · 剂量为原型候选，可减量；完成记录不等于改善。</p>`+report.training.drills.map(d=>`<article class="drill"><h3>${esc(d.goal)}</h3>${items(d.steps)}<p>频率：${esc(d.frequency)} · 剂量：${esc(d.dose)}</p><p>进阶：${esc(d.progression)}</p><p>停止：${esc(d.stop_conditions.join('；'))}</p><p>复测：${esc(d.retest)}</p></article>`).join(''):'<p>本次不提供个体训练负荷。请依据状态补充证据或与线下专业人员确认。</p>';
  const relative=r=>path.relative(path.join(workspace,'public/report'),path.join(root,r.path)).split(path.sep).map(s=>s==='..'?'..':encodeURIComponent(s)).join('/');
  const media=r=>!r?'':r.mime==='video/mp4'?`<video controls preload="metadata" data-media src="${esc(relative(r))}"></video><label>播放速度 <select data-speed><option value="1">1×</option><option value="0.5">0.5×</option><option value="0.25">0.25×</option></select></label>`:`<img data-media src="${esc(relative(r))}" alt="${esc(r.kind==='target_pose'?'通用目标示意，非真实改善结果':'当前证据；'+(report.simulation?'模拟素材':'原片段'))}" loading="lazy">`;
  const evidence=report.evidence.map(e=>`<figure id="ev-${esc(e.evidence_id)}" class="evidence"><figcaption>当前证据${report.simulation?'（模拟）':''} · ${esc(e.evidence_id)} · 原视频 ${e.original_start_s.toFixed(2)}–${e.original_end_s.toFixed(2)}秒</figcaption>${media(e.resource)}<p class="resource-error" hidden>本地证据资源不可用；请恢复资源后再参考相关结论。</p><p>可见：${esc(e.visible_objects.join('、'))}</p></figure>`).join('')||'<p>没有可用的个人证据资源。</p>';
  const footwork=JSON.parse(fs.readFileSync(path.join(plugin,'skills/tennis-training-plan/references/footwork.json'),'utf8'));
  const timeline='<ol class="timeline">'+[...report.timing].sort((a,b)=>(a.original_s??Infinity)-(b.original_s??Infinity)).map(e=>`<li><span>${esc(footwork.events.find(x=>x.kind===e.kind)?.label||e.kind)}</span><strong>${e.original_s===null?'无法判断':e.original_s.toFixed(2)+'秒'}</strong><small>${esc(valueLabels[e.value_state])} ${e.evidence_ids.map(id=>link('ev-'+id,id)).join(' ')}</small></li>`).join('')+'</ol><p class="muted">同一原视频时间轴；事件可以重叠，缺失不等于错误。</p>';
  const metrics=report.metrics.map(m=>`<p><strong>${esc(m.definition)}</strong>：${esc(m.value??m.value_state)} (${esc(m.unit)})<br>参照：${esc(m.reference)}；限制：${esc(m.limitations.join('；'))}</p>`).join('')||'<p>无可用相对指标；不推算真实力、力矩或职业评分。</p>';
  const sources=report.sources.map(s=>`<article id="src-${esc(s.source_id)}" class="source"><h3><a href="${esc(new URL(s.url).href)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a></h3><p>${esc(s.author)} · 核验 ${esc(s.accessed_on)}</p><p>支持：${esc(s.supports)}</p><p>适用：${esc(s.scope)}；限制：${esc(s.limitations)}</p></article>`).join('')||'<p>本次无外部结论；未核验来源不能用作技术判断。</p>';
  const target=report.target?`<h3>通用目标示意 · 非真实改善结果</h3>${media(report.target.resource)}${items([...report.target.features,...report.target.limitations])}`:'<p>未生成通用目标示意；真人概念图默认关闭。</p>';
  const retest=report.training?items([report.training.retest_protocol.camera,report.training.retest_protocol.repetitions,report.training.retest_protocol.ball_task,...report.training.retest_protocol.observations,...report.training.retest_protocol.success_conditions]):'<p>补齐同机位、相近来球和身体反馈后再复测；条件不可比只并列说明差异。</p>';
  const values={SUMMARY:summary,PROBLEMS:problems,TRAINING:training,EVIDENCE:evidence,TIMELINE:timeline,METRICS:metrics,SOURCES:sources,TARGET:target,RETEST:retest,LIMITATIONS:items(report.limitations),SIMULATION:report.simulation?'模拟原型 · 非真实识别/邮件':''};
  let html=fs.readFileSync(path.join(templateRoot,'report.html'),'utf8').replace(/\{\{([A-Z_]+)\}\}/g,(_,key)=>{fail(Object.hasOwn(values,key),'REPORT_TEMPLATE_UNKNOWN');return values[key];});
  fail(!/\{\{[A-Z_]+\}\}/.test(html),'REPORT_TEMPLATE_UNRESOLVED');
  const out=path.join(workspace,'public/report');let outputParent=workspace;
  for(const segment of ['public','report']){outputParent=path.join(outputParent,segment);if(fs.existsSync(outputParent))fail(!fs.lstatSync(outputParent).isSymbolicLink(),'REPORT_OUTPUT_SYMLINK');}
  fs.mkdirSync(out,{recursive:true});fail(fs.realpathSync(out)===out,'REPORT_OUTPUT_SYMLINK');
  for(const name of ['index.html','report.css','report.js']){const target=path.join(out,name);if(fs.existsSync(target))fail(!fs.lstatSync(target).isSymbolicLink(),'REPORT_OUTPUT_SYMLINK');}
  fs.copyFileSync(path.join(templateRoot,'report.css'),path.join(out,'report.css'));if(fs.existsSync(path.join(templateRoot,'report.js')))fs.copyFileSync(path.join(templateRoot,'report.js'),path.join(out,'report.js'));
  const temp=path.join(out,'index.html.tmp');if(fs.existsSync(temp))fail(!fs.lstatSync(temp).isSymbolicLink(),'REPORT_OUTPUT_SYMLINK');fs.writeFileSync(temp,html,{mode:0o600});fs.renameSync(temp,path.join(out,'index.html'));
  return {path:path.relative(root,path.join(out,'index.html')).split(path.sep).join('/'),state:report.state,simulation:report.simulation};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href){
  try{const args=process.argv.slice(2),get=k=>args[args.indexOf(k)+1];fail(args.includes('--input')&&args.includes('--data-root'),'REPORT_ARGUMENTS_REQUIRED');const dataRoot=fs.realpathSync(get('--data-root')),input=fs.realpathSync(get('--input'));fail(input.startsWith(dataRoot+path.sep),'REPORT_INPUT_SCOPE_INVALID');const report=JSON.parse(fs.readFileSync(input,'utf8'));const workspace_ref=`projects/${report.task_id}/v${report.version}`;fail(input.startsWith(path.join(dataRoot,workspace_ref,'public')+path.sep),'REPORT_INPUT_SCOPE_INVALID');const contextFile=path.join(dataRoot,workspace_ref,'public/report-context.json');const context=JSON.parse(fs.readFileSync(contextFile,'utf8'));console.log(JSON.stringify(renderReport(report,{...context,dataRoot,task_id:report.task_id,version:report.version,workspace_ref,simulation:report.simulation})));}catch(error){process.stderr.write(JSON.stringify({code:/^[A-Z_]+$/.test(error.message)?error.message:'REPORT_RENDER_FAILED',stage:'render'})+'\n');process.exitCode=1;}
}
