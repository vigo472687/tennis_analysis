import fs from 'node:fs';
import path from 'node:path';
import {ContractError} from './contracts.js';

function canonical(value:string):string {
  let current=path.resolve(value);const suffix:string[]=[];
  while(!fs.existsSync(current)){
    const parent=path.dirname(current);if(parent===current)throw new ContractError('PATH_UNAVAILABLE');
    suffix.unshift(path.basename(current));current=parent;
  }
  return path.resolve(fs.realpathSync(current),...suffix);
}
function inside(root:string,target:string):boolean {
  const relative=path.relative(root,target);return relative===''||(!relative.startsWith('..'+path.sep)&&relative!=='..'&&!path.isAbsolute(relative));
}

export class PathBoundary {
  private readonly pluginRoot:string;
  private readonly projectRoot:string;
  private readonly selectedMedia:ReadonlyMap<string,string>;
  constructor(pluginRoot:string,projectRoot:string,selectedMedia:ReadonlyMap<string,string>=new Map()){
    this.pluginRoot=canonical(pluginRoot);this.projectRoot=canonical(projectRoot);
    this.selectedMedia=new Map([...selectedMedia].map(([key,value])=>[key,canonical(value)]));
  }
  resolve(scope:'plugin'|'project',reference:string):string {
    if(!reference||reference.includes('\0')||path.isAbsolute(reference)||/^[a-zA-Z]:|^[a-z]+:\/\//.test(reference)||reference.replaceAll('\\','/').split('/').includes('..'))throw new ContractError('PATH_FORBIDDEN');
    const root=scope==='plugin'?this.pluginRoot:this.projectRoot;
    const target=canonical(path.resolve(root,reference));
    if(!inside(root,target))throw new ContractError('PATH_FORBIDDEN');return target;
  }
  media(reference:string):string {
    const selected=this.selectedMedia.get(reference);if(!selected)throw new ContractError('MEDIA_NOT_AUTHORIZED');
    if(!fs.existsSync(selected)||canonical(selected)!==selected||!fs.statSync(selected).isFile())throw new ContractError('MEDIA_UNAVAILABLE');
    return selected;
  }
  publicTask(reference:string,taskId:string,version:number):string {
    if(!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(taskId)||!Number.isInteger(version)||version<1)throw new ContractError('TASK_REFERENCE_INVALID');
    const expected=`projects/${taskId}/v${version}`;
    const normalized=reference.replaceAll('\\','/');
    if(normalized!==expected&&!normalized.startsWith(expected+'/'))throw new ContractError('TASK_REFERENCE_MISMATCH');
    return this.resolve('project',normalized);
  }
}
