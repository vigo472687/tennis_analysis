import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { Ajv } from 'ajv';

test('official SDK is constructible and JSON Schema rejects malformed input', () => {
  const server = new Server({name:'tennis-test',version:'0.1.0'}, {capabilities:{tools:{}}});
  assert.ok(server);
  const validate = new Ajv().compile({type:'object',required:['task_id'],properties:{task_id:{type:'string'}},additionalProperties:false});
  assert.equal(validate({task_id:'task-1'}), true);
  assert.equal(validate({task_id:2}), false);
  assert.equal(validate({task_id:'task-1',raw_video:'unexpected'}), false);
});

test('stdout protocol and stderr diagnostics remain distinct', () => {
  const child=spawnSync(process.execPath,['-e','process.stdout.write("{\\"ok\\":true}\\n");process.stderr.write("diagnostic\\n")'],{encoding:'utf8'});
  assert.equal(child.status,0);
  assert.deepEqual(JSON.parse(child.stdout),{ok:true});
  assert.equal(child.stderr,'diagnostic\n');
});

test('all direct dependencies use exact versions', () => {
  const manifest=JSON.parse(readFileSync('package.json','utf8')) as {dependencies:Record<string,string>;devDependencies:Record<string,string>};
  for(const version of Object.values({...manifest.dependencies,...manifest.devDependencies}))assert.match(version,/^\d+\.\d+\.\d+$/);
});
