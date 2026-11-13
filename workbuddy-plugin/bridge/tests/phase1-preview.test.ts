import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {Phase1Preview} from '../src/phase1-preview.js';

test('archived MPEG-4 preview becomes a cached H.264 derivative without changing its source',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'phase1-preview-'));
  try{
    const source=path.join(dir,'source.mp4');
    execFileSync('ffmpeg',['-v','error','-f','lavfi','-i','color=c=green:s=64x64:d=0.2','-c:v','mpeg4',source]);
    const original=fs.readFileSync(source);const preview=new Phase1Preview(path.join(dir,'cache'));
    const [a,b]=await Promise.all([preview.file(source),preview.file(source)]);assert.equal(a,b);assert.notEqual(a,source);
    assert.equal(execFileSync('ffprobe',['-v','error','-select_streams','v:0','-show_entries','stream=codec_name','-of','csv=p=0',a],{encoding:'utf8'}).trim(),'h264');
    assert.deepEqual(fs.readFileSync(source),original);
    const cached=fs.statSync(a).mtimeMs;assert.equal(await preview.file(source),a);assert.equal(fs.statSync(a).mtimeMs,cached);
    await assert.rejects(()=>preview.file(path.join(dir,'missing.mp4')));
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
