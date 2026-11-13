import test from 'node:test';
import assert from 'node:assert/strict';
import {videoReport} from '../src/video-experience-report.js';

test('offline video observations escape user text and embed only supplied evidence',()=>{
  const html=videoReport({filename:'<script>alert(1)</script>.mp4',stroke:'forehand',handedness:'right',created_at:'2026-11-10',media:{duration_s:4,width:576,height:576},summary:{detection_ratio:1,individual_review_available:true,angle_ranges:{left_elbow:{min:80,max:150,samples:30}}},sourceHash:'source-hash',modelHash:'model-hash',evidence:[{t:1.25,jpeg:'YWJj'}]});
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);assert.match(html,/data:image\/jpeg;base64,YWJj/);assert.match(html,/80° – 150°/);assert.match(html,/1.250 秒/);assert.match(html,/source-hash/);assert.match(html,/模型估计/);
});

test('insufficient pose data does not produce a measurement table or a coaching claim',()=>{
  const html=videoReport({filename:'empty.mp4',stroke:'footwork',handedness:'left',created_at:'2026-11-10',media:{},summary:{individual_review_available:false,detection_ratio:0},sourceHash:'',modelHash:'',evidence:[]});
  assert.doesNotMatch(html,/<table>/);assert.match(html,/没有足够有效的姿态测量/);assert.match(html,/没有可发布的个体证据帧/);
});
