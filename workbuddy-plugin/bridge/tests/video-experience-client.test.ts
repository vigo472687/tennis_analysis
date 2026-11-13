import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {experienceScript} from '../src/video-experience-client.js';

function client(job:Record<string,unknown>={}) {
  const elements=new Map<string,any>();
  const element=(id:string)=>{
    if(!elements.has(id))elements.set(id,{
      elements:[],textContent:'',innerHTML:'',hidden:false,value:'',
      addEventListener(){},pause(){},load(){},removeAttribute(){},
    });
    return elements.get(id);
  };
  const stored=new Map<string,string>();
  const context=vm.createContext({
    document:{getElementById:element,querySelectorAll:()=>[]},
    window:{addEventListener(){}},
    localStorage:{getItem:(key:string)=>stored.get(key),setItem:(key:string,value:string)=>stored.set(key,value),removeItem:(key:string)=>stored.delete(key)},
    fetch:async (url:string)=>({ok:true,json:async()=>url==='/api/experience/videos'?{items:[]}:job}),
    clearInterval(){},clearTimeout(){},setTimeout(){},
  });
  vm.runInContext(experienceScript,context);
  return {context,element,stored};
}

test('a new video keeps training preferences but requires fresh permissions and clears prior results',()=>{
  const {context,element,stored}=client();
  stored.set('tennis-real-active','previous-video');
  element('model-label').textContent='MediaPipe Full · 本机推理完成';
  vm.runInContext(`
    state.form={...state.form,age_band:'youth',guardian:true,consent:true,save:true,retest:true,goal:'Review my movement'};
    state.job={completed:['inspect','pose']};state.pose={frames:[]};state.poseId='previous-video';
    newVideo();
  `,context);
  const state=JSON.parse(vm.runInContext('JSON.stringify(state)',context));
  for(const key of ['guardian','consent','save','retest'])assert.equal(state.form[key],false);
  assert.equal(state.form.goal,'Review my movement');
  assert.equal(state.form.age_band,'youth');
  assert.equal(state.job,null);
  assert.equal(state.pose,null);
  assert.equal(state.poseId,null);
  assert.equal(stored.has('tennis-real-active'),false);
  assert.equal(element('model-label').textContent,'人体姿态 · MediaPipe Full');
  assert.equal(element('primary-action').disabled,true);
});

test('resuming a video does not copy analysis results into the next upload metadata',async()=>{
  const {context}=client({id:'previous-video',filename:'practice.mp4',status:'uploaded',completed:[],
    age_band:'adult',handedness:'left',stroke:'forehand',camera:'side',safety:'clear',
    goal:'Review my movement',background:'Weekly practice',consent:true,save:true,retest:true,
    result:{largeAnalysis:'x'.repeat(30000)},size:1000,simulation:false});
  await vm.runInContext("loadJob('previous-video')",context);
  const form=JSON.parse(vm.runInContext('JSON.stringify(state.form)',context));
  assert.equal(form.handedness,'left');
  assert.equal(form.consent,true);
  assert.equal(form.guardian,false);
  assert.equal('result' in form,false);
  assert.equal('id' in form,false);
  assert.ok(JSON.stringify(form).length<1000);
});
