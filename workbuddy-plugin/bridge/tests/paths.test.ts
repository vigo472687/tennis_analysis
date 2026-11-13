import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {PathBoundary} from '../src/paths.js';

test('path boundaries reject traversal, foreign tasks, absolute and symbolic-link escape',t=>{
  const dir=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'tennis-paths-')));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  for(const name of ['plugin','project','outside'])fs.mkdirSync(path.join(dir,name));
  const selected=path.join(dir,'outside','chosen.mp4');fs.writeFileSync(selected,'selected test media');
  const b=new PathBoundary(path.join(dir,'plugin'),path.join(dir,'project'),new Map([['media/chosen.mp4',selected]]));
  assert.equal(b.media('media/chosen.mp4'),selected);assert.throws(()=>b.media('media/other.mp4'),/MEDIA_NOT_AUTHORIZED/);
  for(const bad of ['../outside','x/../../outside','/etc/passwd','C:\\private','https://example.com/x','..\\outside'])assert.throws(()=>b.resolve('project',bad),/PATH_FORBIDDEN/);
  fs.symlinkSync(path.join(dir,'outside'),path.join(dir,'project','escape'));
  assert.throws(()=>b.resolve('project','escape/chosen.mp4'),/PATH_FORBIDDEN/);
  assert.ok(b.publicTask('projects/task-1/v1/report/report.html','task-1',1).endsWith('report.html'));
  assert.throws(()=>b.publicTask('projects/task-2/v1/report.html','task-1',1),/TASK_REFERENCE_MISMATCH/);
  assert.throws(()=>b.publicTask('projects/task-1/v2/report.html','task-1',1),/TASK_REFERENCE_MISMATCH/);
});

test('new output directories remain within the original authorized root',t=>{
  const dir=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'tennis-root-')));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
  fs.mkdirSync(path.join(dir,'plugin'));fs.mkdirSync(path.join(dir,'project'));fs.mkdirSync(path.join(dir,'outside'));
  const b=new PathBoundary(path.join(dir,'plugin'),path.join(dir,'project'));
  assert.equal(b.resolve('project','new/task/report.html'),path.join(dir,'project/new/task/report.html'));
  fs.rmdirSync(path.join(dir,'project'));fs.symlinkSync(path.join(dir,'outside'),path.join(dir,'project'));
  assert.throws(()=>b.resolve('project','new/report.html'),/PATH_FORBIDDEN/);
});
