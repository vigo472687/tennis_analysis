import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {build} from 'esbuild';
import {generateFixtures} from '../../fixtures/media/generate.mjs';
const here=path.dirname(fileURLToPath(import.meta.url)),plugin=path.resolve(here,'..'),root=path.resolve(plugin,'..');
fs.mkdirSync(path.join(plugin,'bin'),{recursive:true});
await build({entryPoints:[path.join(here,'src/server.ts')],outfile:path.join(plugin,'bin/tennis-bridge.mjs'),bundle:true,platform:'node',target:'node20',format:'esm',banner:{js:"import {createRequire as __createRequire} from 'node:module';const require=__createRequire(import.meta.url);"},logLevel:'warning'});
await build({entryPoints:[path.join(plugin,'skills/tennis-report/scripts/render.mjs')],outfile:path.join(plugin,'bin/tennis-report.mjs'),bundle:true,platform:'node',target:'node20',format:'esm',banner:{js:"import {createRequire as __createRequire} from 'node:module';const require=__createRequire(import.meta.url);"},logLevel:'warning'});
fs.cpSync(path.join(root,'contracts/v1'),path.join(plugin,'contracts/v1'),{recursive:true});
fs.mkdirSync(path.join(plugin,'fixtures/expected'),{recursive:true});
fs.copyFileSync(path.join(root,'fixtures/manifest.json'),path.join(plugin,'fixtures/manifest.json'));
for(const name of fs.readdirSync(path.join(root,'fixtures/expected')))if(name.endsWith('.json'))fs.copyFileSync(path.join(root,'fixtures/expected',name),path.join(plugin,'fixtures/expected',name));
const assets=path.join(plugin,'fixtures/assets'),index=path.join(assets,'generated.json');
const current=fs.existsSync(index)?JSON.parse(fs.readFileSync(index,'utf8')):null;
const manifest=JSON.parse(fs.readFileSync(path.join(root,'fixtures/manifest.json'),'utf8'));
if(current?.config_version!==manifest.config_version||current?.generated?.length!==manifest.samples.length||current.generated.some(s=>!fs.existsSync(path.join(assets,s.media.path))||createHash('sha256').update(fs.readFileSync(path.join(assets,s.media.path))).digest('hex')!==s.media.sha256))generateFixtures(assets);
// A separate generic target illustration. It is never a recovered evidence frame.
execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-f','lavfi','-i','color=c=0xf5f1e5:s=640x360','-vf','drawbox=x=294:y=78:w=36:h=36:c=0x385c52:t=fill,drawbox=x=290:y=120:w=45:h=96:c=0x7a9d90:t=fill,drawbox=x=263:y=220:w=22:h=42:c=0x385c52:t=fill,drawbox=x=335:y=220:w=22:h=42:c=0x385c52:t=fill','-frames:v','1','-threads','1',path.join(assets,'target.png')],{stdio:['ignore','ignore','pipe'],timeout:30000});
// Canned trajectory coordinates are a synthetic display fixture, never a measured path.
execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-i',path.join(assets,'iphone-flat.png'),'-vf','drawbox=x=350:y=206:w=45:h=4:c=0xffc470:t=fill,drawbox=x=391:y=182:w=4:h=28:c=0xffc470:t=fill,drawbox=x=391:y=182:w=50:h=4:c=0xffc470:t=fill,drawbox=x=437:y=150:w=4:h=36:c=0xffc470:t=fill,drawbox=x=437:y=150:w=45:h=4:c=0xffc470:t=fill','-frames:v','1','-threads','1',path.join(assets,'trajectory.png')],{stdio:['ignore','ignore','pipe'],timeout:30000});
const avatars=path.join(plugin,'avatars');fs.mkdirSync(avatars,{recursive:true});
for(const [name,color]of Object.entries({team:'1d6250','team-lead':'466856',vision:'426a86',technique:'698047',research:'6e6185',tactics:'9a6a46',style:'8b626a'})){
  execFileSync('ffmpeg',['-hide_banner','-loglevel','error','-nostdin','-y','-f','lavfi','-i',`color=c=0x${color}:s=512x512`,'-vf','drawbox=x=126:y=126:w=260:h=260:c=0xf1e6c5:t=5,drawbox=x=252:y=126:w=6:h=260:c=0xf1e6c5:t=fill,drawbox=x=126:y=252:w=260:h=6:c=0xf1e6c5:t=fill','-frames:v','1','-threads','1',path.join(avatars,name+'.png')],{stdio:['ignore','ignore','pipe'],timeout:30000});
}
// Verify a relocatable bundle with an official MCP client, without Python or network.
const relocated=fs.mkdtempSync(path.join(os.tmpdir(),'tennis-bundle-'));
try{
  for(const name of ['bin','contracts','fixtures','skills'])fs.cpSync(path.join(plugin,name),path.join(relocated,name),{recursive:true});
  const {Client}=await import('@modelcontextprotocol/sdk/client/index.js');
  const {StdioClientTransport}=await import('@modelcontextprotocol/sdk/client/stdio.js');
  const client=new Client({name:'tennis-build-check',version:'0.1.0'});
  const transport=new StdioClientTransport({command:process.execPath,args:[path.join(relocated,'bin/tennis-bridge.mjs')],cwd:os.tmpdir(),env:{PATH:process.env.PATH||'',TENNIS_DATA_ROOT:path.join(relocated,'data'),TENNIS_BACKEND:'fixture'},stderr:'inherit'});
  try{
    await client.connect(transport);const tools=await client.listTools();if(tools.tools.length!==19)throw new Error('Tool registry mismatch');
    await client.callTool({name:'runtime_status',arguments:{runtime_requirement:'not_required'}});if(fs.existsSync(path.join(relocated,'data')))throw new Error('Idle bridge wrote data');
    const response=await client.callTool({name:'runtime_prepare',arguments:{operation_id:'build-prepare',consent_confirmed:true,target_version:'0.1.0',intent:'prepare',scope_digest:'fixture-local-v1'}});
    if(response.isError)throw new Error('Relocated fixture preparation failed');
  }finally{await client.close();}
}finally{fs.rmSync(relocated,{recursive:true,force:true});}
console.log(JSON.stringify({bundle:'bin/tennis-bridge.mjs',tools:19,fixtures:manifest.samples.length,relocation:'passed',simulation:true,python_artifact:'separate_backend_gate',workbuddy_client:'not_tested'}));
