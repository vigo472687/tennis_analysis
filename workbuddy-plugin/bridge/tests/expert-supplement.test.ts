import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {expertPanel,expertScript} from '../src/expert-panel.js';
import {phase5Script} from '../src/phase5-client.js';

// Execute the shipped browser scripts with deterministic local IO, no paid model requests.
const original={question:'请看我的准备动作',goal:'提前准备',handedness:'left',background:'初学',safety:'discomfort'};
const oldWorkspace='projects/task-old/v1',newWorkspace='projects/task-new/v1';
const completed={id:'old-run',status:'completed',stage:'完成',started_at:new Date().toISOString(),ended_at:new Date().toISOString(),input:original,model:'test',calls:4,members:[],sources:[],result:{answer:'需要补视频',missing_inputs:['清晰单人视频'],next_action:'补拍'}};
function storage(map=new Map<string,string>()){return {map,getItem:(k:string)=>map.get(k)||null,setItem:(k:string,v:string)=>map.set(k,v),removeItem:(k:string)=>map.delete(k)};}
function browser(saved?:Map<string,string>){
 const elements=new Map<string,ElementStub>();
 const listeners=new Map<string,Array<(e:any)=>any>>();
 class ElementStub {
  private _id='';get id(){return this._id;}set id(v:string){this._id=v;if(v)elements.set(v,this);}
  value='';checked=false;disabled=false;hidden=false;textContent='';files:any[]=[];clicks=0;parentElement=this;
  handlers=new Map<string,Array<(e:any)=>any>>();html='';
  constructor(id=''){this.id=id;}
  set innerHTML(v:string){this.html=v;for(const m of v.matchAll(/id="([^"]+)"/g)){elements.set(m[1]!,new ElementStub(m[1]));}}
  get innerHTML(){return this.html;}
  addEventListener(n:string,fn:(e:any)=>any){this.handlers.set(n,[...(this.handlers.get(n)||[]),fn]);}
  async fire(n:string){await Promise.all((this.handlers.get(n)||[]).map(fn=>fn({preventDefault(){}})));}
  click(){this.clicks++;void this.fire('click');}
  append(..._args:any[]){} before(..._args:any[]){} after(..._args:any[]){} focus(){} scrollIntoView(){} removeAttribute(_n:string){} setAttribute(_n:string,_v:string){}
  querySelector(_q:string){return this;}
 }
 const $=(id:string)=>{if(!elements.has(id))elements.set(id,new ElementStub(id));return elements.get(id)!;};
 const calls:Array<{url:string,body:any}>=[];
 const session=storage(saved),local=storage(new Map([['tennis-expert-run','old-run']]));
 const job:any={id:'job-new',pending:false,workspace_ref:newWorkspace,exchange:{response:{ok:true,result:{state:'analyzing'}}}};
 const state={config:{default_data_root:'/local/root'},snapshot:{data_root:'/local/root',running:true}};
 const context=vm.createContext({$,state,console,URL,URLSearchParams,Date,JSON,Math,encodeURIComponent,
  esc:(v:any)=>String(v??''),tree:()=>'',errors:{},api:async()=>({}),refresh:async()=>{},
  localStorage:local,sessionStorage:session,location:{search:'?expert_run=old-run',href:'http://localhost/?expert_run=old-run'},history:{replaceState(){}},
  setInterval:()=>0,setTimeout:()=>0,clearTimeout(){},view:(v:string)=>{(context as any).currentView=v;},
  CustomEvent:class{constructor(public type:string,public options:any={}){} get detail(){return this.options.detail;}},
  window:{addEventListener:(n:string,fn:(e:any)=>any)=>listeners.set(n,[...(listeners.get(n)||[]),fn]),dispatchEvent:(e:any)=>{for(const fn of listeners.get(e.type)||[])void fn(e);}},
  document:{createElement:()=>new ElementStub(),querySelectorAll:()=>[]},
  fetch:async(url:string,options?:any)=>{const body=options?.body?JSON.parse(options.body):null;calls.push({url,body});let data:any;
   if(url.includes('/subjects?'))data={legacy:true,candidates:[],analyzed:[],subjects:[],note:'legacy'};
   else if(url.endsWith('/config'))data={configured:true,provider:'DeepSeek',model:'test'};
   else if(url.includes('/run?'))data=completed;
   else if(url.endsWith('/video-context'))data={context_id:'new-context',workspace_ref:body.workspace_ref,task_id:'task-new',version:1,summary:{media:{width:544,height:960,duration_s:6},metrics:[]},frames:[]};
   else if(url.endsWith('/tasks'))data={items:[{workspace_ref:oldWorkspace,updated_at:new Date().toISOString()},{workspace_ref:newWorkspace,updated_at:new Date().toISOString()}]};
   else if(url.includes('/inspect?'))data={materials:[]};
   else if(url.endsWith('/control'))data={exchange:{response:{ok:true,result:{state:'analyzing'}}}};
   else if(url.endsWith('/active'))data=null;
   else if(url.endsWith('/create')||url.includes('/job?'))data=job;
   else throw new Error('Unexpected fetch '+url);
   return {ok:true,json:async()=>data};}
 });
 vm.runInContext(phase5Script,context);vm.runInContext(expertScript,context);
 const run=(code:string)=>vm.runInContext(code,context);
 const tick=()=>new Promise<void>(resolve=>setImmediate(resolve));
 const init=async()=>{await tick();await run('initializePhase5()');await tick();};
 return {$,run,tick,init,context,calls,session,job,state,fire:async(id:string)=>{$(id).click();await tick();}};
}

test('result supplement carries original fields, resets file/local consent, and cancels without creating a task',async()=>{
 const b=browser();await b.init();b.$('ex-question').value='未提交的新问题';b.$('p5-analysis-consent').checked=true;b.$('p5-file').value='old.mp4';
 await b.fire('ex-result-add-video');
 assert.equal(b.context.currentView,'chain5');assert.equal(b.$('p5-file').value,'');assert.equal(b.$('p5-file').clicks,1);assert.equal(b.$('p5-analysis-consent').checked,false);
 assert.deepEqual(JSON.parse(b.session.getItem('tennis-expert-supplement-v1')!).input,original);
 assert.equal(b.$('p5-supplement-continue').disabled,true);
 assert.equal(b.calls.some(c=>c.url.endsWith('/create')||c.url.endsWith('/start')),false);
});

test('upload must bind its own successful task; return restores all fields and clears old preview/subject consent',async()=>{
 const b=browser();await b.init();await b.fire('ex-result-add-video');
 b.$('p5-analysis-consent').checked=true;b.run("p5.upload={media_ref:'ingest/new.mp4'}");await b.$('p5-create').fire('click');
 await b.run('p5Tasks(p5.job.workspace_ref)');b.run('p5Controls()');assert.equal(b.$('p5-supplement-continue').disabled,false);
 b.$('p5-task').value=oldWorkspace;b.run('p5Controls()');assert.equal(b.$('p5-supplement-continue').disabled,true);
 b.$('p5-task').value=newWorkspace;b.run('p5Controls()');b.$('ex-preview').hidden=false;
 await b.fire('p5-supplement-continue');assert.equal(b.context.currentView,'experts');assert.equal(b.$('ex-question').value,original.question);assert.equal(b.$('ex-hand').value,'left');assert.equal(b.$('ex-safety').value,'discomfort');assert.equal(b.$('ex-preview').hidden,true);assert.equal(b.$('ex-subject-confirmed').checked,false);
 assert.equal(b.calls.filter(c=>c.url.endsWith('/create')).length,1);assert.equal(b.calls.some(c=>c.url.endsWith('/start')),false);
});

test('failed/cancelled upload cannot continue; explicit existing-video choice can continue without a new upload',async()=>{
 const b=browser();await b.init();await b.fire('ex-result-add-video');b.run("supplementDraft.job_id='job-new';supplementDraft.workspace_ref='projects/task-new/v1';p5.job={id:'job-new',pending:false,exchange:{response:{ok:true,result:{state:'cancelled'}}}};p5Controls()");b.$('p5-task').value=newWorkspace;b.run('p5Controls()');assert.equal(b.$('p5-supplement-continue').disabled,true);
 await b.fire('ex-result-existing-video');assert.equal(b.$('p5-supplement-continue').disabled,false);await b.fire('p5-supplement-continue');assert.equal(b.context.currentView,'experts');assert.equal(b.$('ex-question').value,original.question);assert.equal(b.calls.some(c=>c.url.endsWith('/create')||c.url.endsWith('/start')),false);
});

test('busy backend rejects replacement and keeps the draft unchanged',async()=>{
 const b=browser();await b.init();b.run("p5.job={id:'active',pending:true}");await b.fire('ex-result-add-video');assert.equal(b.session.getItem('tennis-expert-supplement-v1'),null);assert.equal(b.$('p5-file').clicks,0);assert.match(b.$('ex-error').textContent,/仍在处理/);
});

test('refresh resumes only saved job, then restores review without creating tasks or remote calls; root switch clears draft',async()=>{
 const saved=new Map([['tennis-expert-supplement-v1',JSON.stringify({root:'/local/root',mode:'upload',input:original,job_id:'job-new',workspace_ref:newWorkspace,phase:'review'})]]);
 const b=browser(saved);await b.init();assert.equal(b.context.currentView,'experts');assert.equal(b.$('ex-question').value,original.question);assert.equal(b.$('ex-safety').value,'discomfort');assert.equal(b.calls.some(c=>c.url.endsWith('/create')||c.url.endsWith('/start')),false);
 b.state.snapshot.data_root='/other/root';b.run('phase5Snapshot()');assert.equal(b.session.getItem('tennis-expert-supplement-v1'),null);assert.equal(b.$('p5-supplement').hidden,true);
});

test('new file invalidates association and requires a fresh local consent',async()=>{
 const b=browser();await b.init();await b.fire('ex-result-existing-video');b.$('p5-analysis-consent').checked=true;await b.$('p5-file').fire('change');const draft=JSON.parse(b.session.getItem('tennis-expert-supplement-v1')!);assert.equal(draft.mode,'upload');assert.equal(draft.workspace_ref,null);assert.equal(b.$('p5-analysis-consent').checked,false);assert.equal(b.$('p5-supplement-continue').disabled,true);
});

test('panel exposes both form and result entry paths',()=>{
 assert.match(expertPanel,/id="ex-add-video"/);assert.match(expertPanel,/id="ex-existing-video"/);
});


test('supplement started before connecting is retained only for the chosen data directory',async()=>{
 const b=browser();await b.init();b.state.snapshot.data_root='';b.run('phase5Snapshot()');await b.fire('ex-add-video');assert.equal(JSON.parse(b.session.getItem('tennis-expert-supplement-v1')!).root,'/local/root');
 b.state.snapshot.data_root='/local/root';b.run('phase5Snapshot()');assert.ok(b.session.getItem('tennis-expert-supplement-v1'));
 b.state.snapshot.data_root='/different';b.run('phase5Snapshot()');assert.equal(b.session.getItem('tennis-expert-supplement-v1'),null);
});
