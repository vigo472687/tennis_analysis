import fs from 'node:fs';
import {Phase1Error} from './phase1-backend.js';

export interface ActionSegment {start_frame:number;end_frame:number;start_s:number;end_s:number;frames:number}
export interface ActionSelection {max_actions:number;segments_found:number;selected_count:number;window_frames:number;selected_frames:number;excluded_frames:number;selection:string;note?:string;selected_segments?:ActionSegment[];details_available:boolean}
export function readActionSelection(file:string):ActionSelection {
  const fail=():never=>{throw new Phase1Error('ACTION_SELECTION_INVALID',422);};
  if(fs.statSync(file).size>128*1024)fail();
  let x:any;try{x=JSON.parse(fs.readFileSync(file,'utf8'));}catch{return fail();}
  if(!x||typeof x!=='object')fail();
  for(const k of ['max_actions','segments_found','selected_count','window_frames','selected_frames','excluded_frames'])if(!Number.isSafeInteger(x[k])||x[k]<0)fail();
  if((x.selected_count===0)!==(x.selected_frames===0))fail();
  if(x.max_actions!==10||x.selected_count!==Math.min(x.segments_found,10)||x.selected_frames+x.excluded_frames!==x.window_frames||typeof x.selection!=='string')fail();
  const details=x.selected_segments!==undefined;
  if(details){
    if(!Array.isArray(x.selected_segments)||x.selected_segments.length!==x.selected_count)fail();
    let previous=-1,total=0;
    for(const s of x.selected_segments){
      if(!s||!Number.isSafeInteger(s.start_frame)||!Number.isSafeInteger(s.end_frame)||s.start_frame<=previous||s.start_frame<0||s.end_frame<s.start_frame||s.end_frame>=x.window_frames||s.frames!==s.end_frame-s.start_frame+1||!Number.isFinite(s.start_s)||!Number.isFinite(s.end_s)||s.start_s<0||s.end_s<s.start_s)fail();
      previous=s.end_frame;total+=s.frames;
    }
    if(total!==x.selected_frames)fail();
  }
  return {...x,details_available:details};
}
