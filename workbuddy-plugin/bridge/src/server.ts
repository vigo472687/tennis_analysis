import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {createHash,randomUUID} from 'node:crypto';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {CallToolRequestSchema,ListToolsRequestSchema} from '@modelcontextprotocol/sdk/types.js';
import {CONTRACT_VERSION,ContractRegistry,ContractError} from './contracts.js';
import {RuntimeStore} from './runtime-state.js';
import {FixtureBackend} from './fixture-backend.js';
import {ProcessClient} from './process-client.js';
import {ToolDispatcher} from './tools.js';
import {PathBoundary} from './paths.js';
export interface BridgeOptions {pluginRoot:string;dataRoot:string;mode:'fixture'|'python';}
export function createBridge(options:BridgeOptions){
  const contractRoot=path.join(options.pluginRoot,'contracts/v1'),fixtureRoot=path.join(options.pluginRoot,'fixtures');
  const registry=new ContractRegistry(contractRoot),store=new RuntimeStore(registry,options.dataRoot,options.mode);
  const scope=options.mode+'-local-v1',boundary=new PathBoundary(options.pluginRoot,options.dataRoot);
  const fixture=new FixtureBackend(registry,{fixtureRoot,dataRoot:options.dataRoot,assetRoot:path.join(fixtureRoot,'assets')});
  const artifactRoot=path.join(options.pluginRoot,'backend');
  const python=new ProcessClient(registry,{command:'uv',args:['run','--frozen','--offline','--project',artifactRoot,'tennis-backend','--stdio'],cwd:options.pluginRoot,env:{TENNIS_CONTRACT_ROOT:contractRoot,TENNIS_FIXTURE_ROOT:fixtureRoot,TENNIS_DATA_ROOT:options.dataRoot}});
  let fixtureRunning=true;
  const health=async()=>{
    if(options.mode==='fixture')return [{code:'fixture_ready',passed:fixtureRunning}];
    const reply=await python.call({contract_version:CONTRACT_VERSION,request_id:'health-'+randomUUID(),operation_id:null,method:'runtime.status',params:{runtime_requirement:'required'},simulation:false});
    if(!reply.ok)throw new ContractError('BACKEND_HEALTH_FAILED');return [{code:'python_contract_ready',passed:true}];
  };
  const verify=async(version:string)=>{
    if(version!=='0.1.0')throw new ContractError('ARTIFACT_VERSION_UNAVAILABLE');
    if(options.mode==='fixture'){
      for(const sample of JSON.parse(fs.readFileSync(path.join(fixtureRoot,'manifest.json'),'utf8')).samples as {fixture_id:string}[])if(!fs.existsSync(path.join(fixtureRoot,'assets',sample.fixture_id+'.mp4')))throw new ContractError('FIXTURE_ASSET_MISSING');
      fixtureRunning=true;return [{code:'fixture_assets_ready',passed:true}];
    }
    const manifestPath=path.join(artifactRoot,'artifact.json');if(!fs.existsSync(manifestPath))throw new ContractError('PYTHON_ARTIFACT_MISSING');
    const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8')) as {version:string;files:Record<string,string>};
    if(manifest.version!==version||!Object.keys(manifest.files).length)throw new ContractError('ARTIFACT_VERSION_MISMATCH');
    for(const [relative,expected]of Object.entries(manifest.files)){
      const file=boundary.resolve('plugin','backend/'+relative);
      if(!/^[a-f0-9]{64}$/.test(expected)||createHash('sha256').update(fs.readFileSync(file)).digest('hex')!==expected)throw new ContractError('ARTIFACT_HASH_MISMATCH');
    }
    await python.start();return health();
  };
  const closeBackend=async()=>{fixture.close();fixtureRunning=false;await python.close();};
  const tools=new ToolDispatcher(registry,{contractRoot,store,authorizedScope:scope,simulation:true,backend:options.mode==='fixture'?fixture:python,
    prepare:{checkSpace:()=>{let root=options.dataRoot;while(!fs.existsSync(root))root=path.dirname(root);const s=fs.statfsSync(root);return s.bavail*s.bsize>=64*1024*1024;},verifyArtifact:verify},
    start:{isRunning:()=>options.mode==='fixture'?fixtureRunning:python.running,startBackend:async()=>{if(options.mode==='fixture')fixtureRunning=true;else await python.start();},verifyBackend:health},
    repair:{authorizedScope:scope,repairArtifact:async version=>({version:version||'0.1.0',checks:await verify(version||'0.1.0')})},
    upgrade:{authorizedScope:scope,verifyTarget:verify,restorePrevious:async version=>{await verify(version);return true;}},
    uninstall:{projectDeletionConfirmed:false,stopBackend:closeBackend,removeComponents:async()=>[{kind:'components',removed:true}],deleteProjects:async()=>[]}
  });
  const server=new Server({name:'tennis-workbuddy',version:'0.1.0'},{capabilities:{tools:{}}});
  server.setRequestHandler(ListToolsRequestSchema,async()=>({tools:tools.definitions()}));
  server.setRequestHandler(CallToolRequestSchema,async request=>{const response=await tools.call(request.params.name,request.params.arguments||{});return {content:[{type:'text' as const,text:JSON.stringify(response)}],structuredContent:response as unknown as Record<string,unknown>,isError:!response.ok};});
  return {server,tools,store,python,close:async()=>{await closeBackend();await server.close();}};
}
export async function main(){
  const here=path.dirname(fileURLToPath(import.meta.url));
  const pluginRoot=process.env.TENNIS_PLUGIN_ROOT||path.resolve(here,path.basename(here)==='bin'?'..':'../../..');
  const dataRoot=process.env.TENNIS_DATA_ROOT||path.join(os.homedir(),'Library/Application Support/tennis-workbuddy');
  const mode=process.env.TENNIS_BACKEND==='python'?'python':'fixture';
  const bridge=createBridge({pluginRoot,dataRoot,mode});
  const transport=new StdioServerTransport(process.stdin,process.stdout,{maxBufferSize:1048576});
  transport.onclose=()=>{void bridge.close();};
  for(const signal of ['SIGINT','SIGTERM'] as const)process.once(signal,()=>{void bridge.close().finally(()=>process.exit(0));});
  await bridge.server.connect(transport);
}
if(process.argv[1]&&import.meta.url===pathToFileURL(fs.realpathSync(process.argv[1])).href)main().catch(()=>{process.stderr.write('{"stage":"runtime","code":"BRIDGE_START_FAILED"}\n');process.exitCode=1;});
