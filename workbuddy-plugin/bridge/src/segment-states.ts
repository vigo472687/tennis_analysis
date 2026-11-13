import fs from 'node:fs';
import {Phase1Error} from './phase1-backend.js';
import type {ActionSelection} from './action-selection.js';
export interface SegmentState {segment:[number,number];frames:number;status:'posed'|'deferred_multi_subject'|'skipped_no_subject';pose_frames?:number}
export function readSegmentStates(file:string,selection:ActionSelection|null):SegmentState[]{
  const fail=():never=>{throw new Phase1Error('SEGMENT_STATES_INVALID',422);};
  if(fs.statSync(file).size>128*1024)fail();let rows:any;try{rows=JSON.parse(fs.readFileSync(file,'utf8'));}catch{return fail();}
  if(!selection?.details_available||!Array.isArray(rows)||rows.length!==selection.selected_count)fail();
  let previous=-1;for(const [i,r] of rows.entries()){
    const s=selection!.selected_segments![i];
    if(!r||!Array.isArray(r.segment)||r.segment.length!==2||!r.segment.every(Number.isSafeInteger)||r.segment[0]<=previous||r.segment[0]!==s?.start_frame||r.segment[1]!==s?.end_frame||r.frames!==s?.frames||!['posed','deferred_multi_subject','skipped_no_subject'].includes(r.status))fail();
    if(r.status==='posed'&&(!Number.isSafeInteger(r.pose_frames)||r.pose_frames<0||r.pose_frames>r.frames))fail();
    if(r.status!=='posed'&&r.pose_frames!==undefined&&r.pose_frames!==0)fail();previous=r.segment[1];
  }return rows;
}
export function segmentAt(states:SegmentState[]|null,frame:number):SegmentState|undefined{return states?.find(s=>frame>=s.segment[0]&&frame<=s.segment[1]);}
