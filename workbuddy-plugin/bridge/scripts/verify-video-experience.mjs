import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const [source,expectation='person',origin='http://127.0.0.1:4178']=process.argv.slice(2);
assert.ok(source,'Usage: node scripts/verify-video-experience.mjs VIDEO [person|empty] [ORIGIN]');
const metadata={filename:path.basename(source),age_band:'adult',guardian:false,handedness:'right',stroke:'footwork',goal:'Real video pipeline verification',background:'Public validation footage; not a tennis accuracy evaluation',camera:'front',safety:'clear',consent:true,save:true,retest:true};
const created=await fetch(origin+'/api/experience/videos',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Video-Metadata':Buffer.from(JSON.stringify(metadata)).toString('base64url')},body:fs.readFileSync(source)});
assert.equal(created.status,201);const job=await created.json();const route=origin+'/api/experience/videos/'+job.id;
const phases=[];let snapshot;
for(const stage of ['inspect','pose','evidence']){
  const started=await fetch(route+'/'+stage,{method:'POST'});assert.equal(started.status,202,await started.text());
  for(let attempt=0;attempt<240;attempt++){
    await new Promise(resolve=>setTimeout(resolve,400));
    snapshot=await fetch(route).then(response=>response.json());
    if(snapshot.status!=='running')break;
  }
  assert.equal(snapshot.status,'completed',JSON.stringify(snapshot));phases.push({stage,status:snapshot.status});
}
assert.equal(snapshot.simulation,false);assert.equal(snapshot.result.simulation,false);
const summary=snapshot.result.pose.summary;
const pose=await fetch(route+'/resources/pose.json').then(response=>response.json());
assert.ok(pose.frames.length>0);assert.equal(pose.simulation,false);
if(expectation==='person'){
  assert.equal(summary.individual_review_available,true);assert.ok(summary.detection_ratio>.35);
  assert.ok(Object.keys(summary.angle_ranges).length>0);assert.ok(snapshot.result.evidence.length>0);
}else{assert.equal(summary.individual_review_available,false);assert.deepEqual(summary.angle_ranges,{});assert.deepEqual(summary.segments,[]);assert.deepEqual(snapshot.result.evidence,[]);}
const media=await fetch(route+'/resources/preview.mp4',{headers:{Range:'bytes=0-63'}});assert.equal(media.status,206);assert.equal((await media.arrayBuffer()).byteLength,64);
const exported=await fetch(route+'/export').then(response=>response.json());assert.equal(exported.simulation,false);assert.equal(exported.pose.frames.length,pose.frames.length);
const report=await fetch(route+'/report');assert.equal(report.status,200);assert.match(await report.text(),/视频观察记录/);
console.log(JSON.stringify({id:job.id,source:path.basename(source),expectation,phases,simulation:false,summary:{...summary,motion_samples:summary.motion.length,motion:undefined},evidence:snapshot.result.evidence.length,range_playback:'pass',json_export:'pass',offline_report:'pass'},null,2));
