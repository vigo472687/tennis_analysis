import test from 'node:test';
import assert from 'node:assert/strict';
import {SafeLogger,type LogEvent} from '../src/logging.js';

test('logs whitelist metadata and exclude private inputs, secrets and paths',()=>{
  const lines:string[]=[];const logger=new SafeLogger(line=>lines.push(line));
  const input={task_id:'task-1',operation_id:'op-1',stage:'analysis',code:'ANALYSIS_FAILED',outcome:'failure',simulation:true,api_key:'SECRET123',chat:'private full chat',path:'/Users/private/video.mov'} as LogEvent;
  logger.log(input);const line=lines[0]!;const value=JSON.parse(line) as Record<string,unknown>;
  assert.equal(value.task_id,'task-1');assert.equal(value.code,'ANALYSIS_FAILED');assert.equal(lines.length,1);
  for(const forbidden of ['SECRET123','private full chat','/Users/private'])assert.ok(!line.includes(forbidden));
});
test('malformed identifiers and diagnostic messages cannot inject log lines',()=>{
  const lines:string[]=[];new SafeLogger(line=>lines.push(line)).log({task_id:'sk-secret',operation_id:'op\nleak',stage:'/private/location',code:'message with SECRET',outcome:'failure',simulation:true,duration_ms:-1});
  const value=JSON.parse(lines[0]!) as Record<string,unknown>;
  assert.equal(value.task_id,null);assert.equal(value.operation_id,null);assert.equal(value.code,'REDACTED');assert.equal(value.duration_ms,null);
});
