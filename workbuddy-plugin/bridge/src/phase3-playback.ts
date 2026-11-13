export interface PoseFrame {frame:number;pts_s:number;keypoints:number[][]|null;valid:boolean[]|null;failure:string|null;subject_overlap:boolean|null;subject_box:number[]|null}
export interface SkeletonSpec {keypoints:{index:number;name:string;color:number[]}[];skeleton:{index:number;from:string;to:string;color:number[]}[]}
export function poseFrameIndex(frames:PoseFrame[],time:number,frameDuration:number):number {
  if(!frames.length||!Number.isFinite(time)||time<frames[0]!.pts_s-0.00001)return -1;
  let low=0,high=frames.length-1;while(low<=high){const mid=(low+high)>>1;if(frames[mid]!.pts_s<=time+0.00001)low=mid+1;else high=mid-1;}
  if(high<0||time-frames[high]!.pts_s>=frameDuration+0.0001)return -1;return high;
}
export function poseDrawing(frame:PoseFrame|undefined,spec:SkeletonSpec){
  const empty={points:[] as {index:number;x:number;y:number;valid:boolean;color:number[]}[],links:[] as {from:number[];to:number[];color:number[]}[],valid:0,failure:frame?.failure||'outside_pose_window'};
  if(!frame||frame.failure!==null||frame.subject_overlap!==true||!frame.keypoints||!frame.valid)return empty;
  const indices=new Map(spec.keypoints.map(p=>[p.name,p.index]));const points=spec.keypoints.map(p=>({index:p.index,x:frame.keypoints![p.index]![0]!,y:frame.keypoints![p.index]![1]!,valid:frame.valid![p.index]!,color:p.color}));
  const links=spec.skeleton.flatMap(l=>{const a=indices.get(l.from),b=indices.get(l.to);return a===undefined||b===undefined||!frame.valid![a]||!frame.valid![b]?[]:[{from:frame.keypoints![a]!,to:frame.keypoints![b]!,color:l.color}];});
  return {points,links,valid:frame.valid.filter(Boolean).length,failure:null};
}
import {segmentAt,type SegmentState} from './segment-states.js';
export function segmentPoseDrawing(frame:PoseFrame|undefined,spec:SkeletonSpec|null,states:SegmentState[]|null,videoFrame:number){
  const segment=segmentAt(states,videoFrame);
  if(segment&&segment.status!=='posed')return {points:[],links:[],valid:0,failure:segment.status};
  if(!spec)return {points:[],links:[],valid:0,failure:'outside_pose_window'};
  return poseDrawing(frame,spec);
}
export const phase3PlaybackScript=poseFrameIndex.toString()+'\n'+poseDrawing.toString()+'\n'+segmentAt.toString()+'\n'+segmentPoseDrawing.toString();
