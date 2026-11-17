import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';

const manifest=JSON.parse(fs.readFileSync(new URL('../manifest.json',import.meta.url),'utf8'));
function inspect(file) {
  const bytes=fs.readFileSync(file);
  return {path:path.basename(file),sha256:createHash('sha256').update(bytes).digest('hex'),size_bytes:bytes.length};
}
function ffmpeg(args) {
  execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y',...args],{timeout:60000,stdio:['ignore','ignore','pipe']});
}

export function generateFixtures(outputDir,ids=manifest.samples.map(s=>s.fixture_id)) {
  fs.mkdirSync(outputDir,{recursive:true});
  const generated=[];
  for(const id of ids){
    const sample=manifest.samples.find(s=>s.fixture_id===id);
    if(!sample)throw new Error('Unknown fixture ID');
    const file=path.join(outputDir,`${id}.mp4`);
    if(id==='damaged'){
      fs.writeFileSync(file,Buffer.from('SYNTHETIC_DAMAGED_MEDIA_NOT_A_REAL_VIDEO'));
      generated.push({fixture_id:id,media:inspect(file),frame:null});continue;
    }
    const filters=['drawbox=x=70:y=65:w=500:h=260:c=white@0.6:t=2','drawbox=x=70:y=195:w=500:h=2:c=white@0.6:t=fill'];
    if(sample.visible_objects.includes('body'))filters.push('drawbox=x=280:y=95:w=36:h=36:c=0xf1d26b:t=fill','drawbox=x=270:y=135:w=55:h=90:c=0x78a5db:t=fill');
    if(sample.visible_objects.includes('feet'))filters.push('drawbox=x=266:y=228:w=22:h=32:c=0xe191a2:t=fill','drawbox=x=310:y=228:w=22:h=32:c=0xe191a2:t=fill');
    if(sample.visible_objects.includes('racket'))filters.push('drawbox=x=348:y=148:w=8:h=75:c=0xe9e9e9:t=fill');
    if(sample.visible_objects.includes('ball'))filters.push('drawbox=x=415:y=180:w=10:h=10:c=0xe2ef64:t=fill');
    ffmpeg(['-f','lavfi','-i',`color=c=0x1f5147:s=640x360:r=30:d=${sample.duration_s}`,'-vf',filters.join(','),'-an','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-threads','1','-metadata','comment=SYNTHETIC FIXTURE - NOT MEASURED TENNIS',file]);
    const frame=path.join(outputDir,`${id}.png`);
    ffmpeg(['-ss','1.2','-i',file,'-frames:v','1','-threads','1',frame]);
    generated.push({fixture_id:id,media:inspect(file),frame:inspect(frame)});
    if(id==='iphone-flat')ffmpeg(['-i',file,'-c','copy',path.join(outputDir,'iphone-flat.mov')]);
  }
  const result={config_version:manifest.config_version,simulation:true,source_kind:'synthetic',generated};
  fs.writeFileSync(path.join(outputDir,'generated.json'),JSON.stringify(result,null,2)+'\n');
  return result;
}

if(process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url){
  const args=process.argv.slice(2);const outIndex=args.indexOf('--out');
  if(outIndex<0||!args[outIndex+1])throw new Error('Usage: node generate.mjs --out DIRECTORY [--ids id,id]');
  const idsIndex=args.indexOf('--ids');const ids=idsIndex<0?undefined:args[idsIndex+1]?.split(',');
  const result=generateFixtures(path.resolve(args[outIndex+1]),ids);
  console.log(JSON.stringify({generated:result.generated.length,simulation:true,config_version:result.config_version}));
}
