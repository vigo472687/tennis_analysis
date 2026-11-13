import fs from 'node:fs';
import path from 'node:path';

export type StoryCoverage='executable'|'partial'|'external';

export interface UserStoryEntry {
  id:string;
  title:string;
  priority:string;
  group:string;
  card:string;
  confirmations:string[];
  outcome:string;
  requirements:string[];
  coverage:StoryCoverage;
  scenario_id:string|null;
  boundary:string;
}

export interface ScenarioStep {
  id:string;
  label:string;
  action:string;
  detail:string;
  expected:string;
}

export interface StoryScenario {
  id:string;
  title:string;
  summary:string;
  story_ids:string[];
  default_mode:'python'|'fixture';
  fixture_id:string|null;
  steps:ScenarioStep[];
}

export interface FixtureEntry {
  fixture_id:string;
  device_scenario:string;
  duration_s:number;
  stroke_kind:string;
  expected_state:string;
  expected_quality:string;
  issue_codes:string[];
  missing_objects:string[];
  subject_candidates:string[];
  requires_subject_selection:boolean;
  mirror_conflict:boolean;
  source_note:string;
  media_ref:string;
}

const scenarios:StoryScenario[]=[
  {
    id:'runtime-lifecycle',title:'本地运行环境与按需启动',summary:'验证准备、启动、状态恢复、修复入口与组件/项目数据边界。',
    story_ids:['US-007','US-008','US-009','US-016'],default_mode:'python',fixture_id:null,
    steps:[
      {id:'start',label:'启动真实后端',action:'session_start',detail:'启动 Python 进程并完成健康检查。',expected:'ready，13 项环境检查通过'},
      {id:'status',label:'读取运行状态',action:'runtime_status',detail:'通过冻结契约读取当前运行状态。',expected:'backend=python，simulation=false'},
      {id:'stop',label:'停止本地进程',action:'session_stop',detail:'关闭子进程但保留项目数据和已验证版本。',expected:'lifecycle=stopped，state=installed_stopped'},
      {id:'resume',label:'按需恢复',action:'session_start',detail:'再次启动并验证原有运行环境。',expected:'恢复 ready，不重复准备组件'}
    ]
  },
  {
    id:'happy-analysis',title:'标准视频分析与证据链路',summary:'从标准固定样本创建任务，逐步查看质量、动作、时序、指标、证据资源和目标骨架。',
    story_ids:['US-002','US-003','US-010','US-020','US-021','US-022','US-023','US-024','US-032'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动固定样本后端',action:'session_start',detail:'使用 Fixture 完成端到端工具验证；结果始终标记 simulation。',expected:'fixture ready，simulation=true'},
      {id:'create',label:'创建分析任务',action:'create_primary',detail:'提交用户信息、用途授权、主路由和固定样本。',expected:'返回 task_id/version'},
      {id:'wait',label:'等待任务终态',action:'wait_primary',detail:'轮询真实任务阶段，不显示虚假精确百分比。',expected:'complete 或明确降级状态'},
      {id:'quality',label:'读取质量报告',action:'quality_primary',detail:'查看兼容性、对象可见性和重拍指引。',expected:'iphone-flat 为 pass'},
      {id:'strokes',label:'读取动作片段',action:'strokes_primary',detail:'列出动作类型、时间范围、阶段和置信度。',expected:'forehand 片段'},
      {id:'timing',label:'读取动作时序',action:'timing_primary',detail:'展示可见事件与 not_visible 事件。',expected:'相对事件顺序，不输出绝对力量'},
      {id:'metrics',label:'读取相对指标',action:'metrics_primary',detail:'查看证据支持的相对指标及限制。',expected:'仅相对时序/连续性指标'},
      {id:'frame',label:'生成证据帧',action:'render_frame',detail:'根据质量报告中的 evidence_id 生成证据资源。',expected:'evidence_frame 可安全预览'},
      {id:'trajectory',label:'生成轨迹资源',action:'render_trajectory',detail:'生成与证据帧区分的轨迹图。',expected:'trajectory 资源与独立哈希'},
      {id:'slow',label:'生成慢动作',action:'render_slow',detail:'生成慢动作证据片段。',expected:'evidence_clip 可播放'},
      {id:'target',label:'生成目标骨架',action:'target_pose',detail:'生成明确标记为教学示意的目标姿态。',expected:'target_pose，不进入当前动作测量'}
    ]
  },
  {
    id:'compatibility-boundary',title:'兼容失败、重拍与部分结果',summary:'用受控固定样本验证 unsupported、reshoot_required 和 partial 的差异。',
    story_ids:['US-004','US-012','US-015'],default_mode:'fixture',fixture_id:'native-panorama',
    steps:[
      {id:'start',label:'启动后端',action:'session_start',detail:'准备固定样本分析环境。',expected:'ready'},
      {id:'create',label:'创建边界任务',action:'create_primary',detail:'可切换全景、超长、人物过小、缺脚/球/球拍等样本。',expected:'返回与样本一致的终态'},
      {id:'wait',label:'读取任务状态',action:'wait_primary',detail:'保留真实状态，不将失败包装为完整分析。',expected:'unsupported / reshoot_required / partial'},
      {id:'quality',label:'查看失败原因',action:'quality_primary',detail:'逐项显示问题、不可用对象和重拍建议。',expected:'问题代码与 fixture manifest 一致'}
    ]
  },
  {
    id:'youth-consent',title:'青少年与分用途授权',summary:'验证监护同意、分析/复测/诊断用途分离，以及拒绝后的结构化错误。',
    story_ids:['US-005','US-006'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动 fixture 对照',action:'session_start',detail:'使用明确标记的模拟模式验证授权规则。',expected:'SIMULATION'},
      {id:'reject',label:'拒绝缺少监护同意',action:'create_youth_rejected',detail:'青少年任务不提供 guardian_simulated。',expected:'GUARDIAN_CONFIRMATION_REQUIRED'},
      {id:'accept',label:'确认监护授权',action:'create_youth_confirmed',detail:'仅在明确监护同意后创建任务。',expected:'任务创建成功'},
      {id:'wait',label:'读取任务终态',action:'wait_primary',detail:'确认授权只影响允许的用途。',expected:'complete'}
    ]
  },
  {
    id:'subject-selection',title:'多人画面对象确认',summary:'先验证不猜测分析对象，再由用户明确选择 person-1。',
    story_ids:['US-014'],default_mode:'fixture',fixture_id:'multiple-subjects',
    steps:[
      {id:'start',label:'启动 fixture 对照',action:'session_start',detail:'准备多人固定样本。',expected:'SIMULATION'},
      {id:'pause',label:'创建未选对象任务',action:'create_primary',detail:'subject_id 保持为空。',expected:'checking_input'},
      {id:'quality',label:'读取对象候选',action:'quality_primary',detail:'显示 person-1 / person-2，不自动选择。',expected:'subject_selection_required'},
      {id:'confirm',label:'确认主对象',action:'create_subject_confirmed',detail:'以新 operation ID 明确选择 person-1。',expected:'新任务进入分析'},
      {id:'wait',label:'确认绑定结果',action:'wait_primary',detail:'后续证据只绑定确认对象。',expected:'partial 或 complete，subject_id=person-1'}
    ]
  },
  {
    id:'cancel-recovery',title:'长任务取消与保留状态',summary:'在 fixture 延迟窗口内取消任务，检查状态、检查点和重复请求边界。',
    story_ids:['US-013'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动延迟 fixture',action:'session_start',detail:'fixture 模式提供受控处理窗口。',expected:'SIMULATION'},
      {id:'create',label:'创建处理中任务',action:'create_primary',detail:'创建后立即进入 analyzing。',expected:'analyzing'},
      {id:'cancel',label:'取消任务',action:'cancel_primary',detail:'取消不删除任务，保留已完成检查点。',expected:'cancelled'},
      {id:'status',label:'复查保留结果',action:'status_primary',detail:'再次读取同一任务。',expected:'状态稳定，不重复创建'}
    ]
  },
  {
    id:'safety-stop',title:'疼痛输入与安全停止',summary:'验证高风险输入不会继续生成动作训练链路。',
    story_ids:['US-045'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动 fixture 对照',action:'session_start',detail:'准备安全规则测试。',expected:'SIMULATION'},
      {id:'create',label:'提交疼痛提示',action:'create_safety_stop',detail:'user.safety=pain。',expected:'safety_stop'},
      {id:'status',label:'确认终态',action:'status_primary',detail:'读取停止原因对应的任务状态。',expected:'不进入动作分析'}
    ]
  },
  {
    id:'retest-compare',title:'复测与相对变化比较',summary:'创建两次相同条件任务并比较，再创建条件不同的反例。',
    story_ids:['US-043','US-046'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动 fixture 对照',action:'session_start',detail:'准备可重复的固定输入。',expected:'SIMULATION'},
      {id:'baseline',label:'创建基线任务',action:'create_primary',detail:'第一轮 iphone-flat。',expected:'基线 task_id'},
      {id:'wait-base',label:'完成基线',action:'wait_primary',detail:'等待基线任务终态。',expected:'complete'},
      {id:'retest',label:'创建复测任务',action:'create_secondary',detail:'保持同一 fixture、动作和条件。',expected:'第二个 task_id'},
      {id:'wait-retest',label:'完成复测',action:'wait_secondary',detail:'等待复测任务终态。',expected:'complete'},
      {id:'compare',label:'比较两次任务',action:'compare_tasks',detail:'只返回相对变化和可比性。',expected:'comparable=true'},
      {id:'counter',label:'创建不可比反例',action:'create_incomparable',detail:'改用 gopro-flat 验证设备/动作差异。',expected:'第三个任务'},
      {id:'compare-counter',label:'验证不可比边界',action:'compare_incomparable',detail:'不制造趋势。',expected:'comparable=false，changes 为空'}
    ]
  },
  {
    id:'review-export-delete',title:'复核材料、脱敏导出与删除',summary:'生成最小证据资源、脱敏诊断包，并验证删除后的不可访问状态。',
    story_ids:['US-044','US-060','US-061','US-062'],default_mode:'fixture',fixture_id:'iphone-flat',
    steps:[
      {id:'start',label:'启动 fixture 对照',action:'session_start',detail:'准备本地反馈材料。',expected:'SIMULATION'},
      {id:'create',label:'创建诊断任务',action:'create_primary',detail:'授权 analysis、retest、diagnostics。',expected:'任务创建成功'},
      {id:'wait',label:'完成分析',action:'wait_primary',detail:'等待资源可生成。',expected:'complete'},
      {id:'quality',label:'读取证据目录',action:'quality_primary',detail:'取得 evidence_id。',expected:'至少一条证据'},
      {id:'render',label:'生成最小证据帧',action:'render_frame',detail:'只生成选中的证据资源。',expected:'evidence_frame'},
      {id:'export',label:'导出脱敏诊断包',action:'export_review',detail:'逐项选择资源并使用 diagnostics 授权。',expected:'redacted=true，manifest 可查看'},
      {id:'delete',label:'删除任务数据',action:'delete_primary',detail:'明确确认后删除任务及派生资源。',expected:'complete=true，external_copies_uncontrolled=true'},
      {id:'verify',label:'验证删除终态',action:'status_primary',detail:'再次读取任务状态。',expected:'deleted 或 TASK_NOT_FOUND'}
    ]
  }
];

const scenarioByStory=new Map(scenarios.flatMap(scenario=>scenario.story_ids.map(id=>[id,scenario.id] as const)));
const partialStories=new Set(['US-003','US-006','US-009','US-010','US-044','US-060','US-061']);
const boundaryByStory:Record<string,string>={
  'US-003':'工作台只使用项目固定样本，不提供任意真人视频上传入口。',
  'US-006':'可验证 Contract 用途授权；授权撤回与 WorkBuddy 交互不在本工作台内。',
  'US-009':'可验证卸载、保留数据和同版本状态；没有可下载的新版本用于真实升级演练。',
  'US-010':'可验证主路由/辅助路由字段约束；自然语言路由澄清属于 WorkBuddy。',
  'US-044':'可验证脱敏本地导出；WorkBuddy 分享链接、有效期和撤回不在本工作台内。',
  'US-060':'可验证任务、报告版本和诊断材料；复核表单编排属于 WorkBuddy。',
  'US-061':'可验证本地脱敏包；邮件预览与发送属于 WorkBuddy 或人工步骤。'
};

function coverageFor(id:string):{coverage:StoryCoverage;scenario_id:string|null;boundary:string}{
  const scenario_id=scenarioByStory.get(id)||null;
  if(scenario_id){
    const coverage=partialStories.has(id)?'partial':'executable';
    return {coverage,scenario_id,boundary:boundaryByStory[id]||'使用固定样本和冻结契约验证；分析输出可能为 simulation，不代表真人视频模型效果。'};
  }
  return {coverage:'external',scenario_id:null,boundary:'该故事主要依赖 WorkBuddy 对话、研究、报告或训练编排；工作台仅保留需求与边界，不生成假执行结果。'};
}

export function storyCatalog(repoRoot:string):{source:string;stories:UserStoryEntry[];scenarios:StoryScenario[]}{
  const source=path.join(repoRoot,'002 PRD','网球姿态捕捉与训练建议应用-用户故事-V02.md');
  const lines=fs.readFileSync(source,'utf8').split(/\r?\n/);
  const stories:UserStoryEntry[]=[];
  let group='';
  for(let index=0;index<lines.length;index+=1){
    const groupMatch=lines[index]!.match(/^## \d+\.\s+(.+)$/);if(groupMatch){group=groupMatch[1]!;continue;}
    const storyMatch=lines[index]!.match(/^### (US-\d+)\s+(.+?)\s+`(P\d)`$/);if(!storyMatch)continue;
    const block:string[]=[];let cursor=index+1;
    while(cursor<lines.length&&!/^### US-\d+/.test(lines[cursor]!)){block.push(lines[cursor]!);cursor+=1;}
    const card=block.find(line=>line.startsWith('**Card**：'))?.slice('**Card**：'.length).trim()||'';
    const confirmations=block.filter(line=>/^- \[ \] /.test(line)).map(line=>line.replace(/^- \[ \] /,'').trim());
    const outcome=block.find(line=>line.startsWith('**完成结果**：'))?.slice('**完成结果**：'.length).trim()||'';
    const requirements=(block.find(line=>line.startsWith('**关联需求**：'))?.match(/FR-\d+/g)||[]);
    const coverage=coverageFor(storyMatch[1]!);
    stories.push({id:storyMatch[1]!,title:storyMatch[2]!,priority:storyMatch[3]!,group,card,confirmations,outcome,requirements,...coverage});
    index=cursor-1;
  }
  return {source:path.relative(repoRoot,source),stories,scenarios:structuredClone(scenarios)};
}

export function fixtureCatalog(repoRoot:string):FixtureEntry[]{
  const manifest=JSON.parse(fs.readFileSync(path.join(repoRoot,'fixtures','manifest.json'),'utf8')) as {samples:FixtureEntry[]};
  return manifest.samples.map(sample=>structuredClone(sample));
}
