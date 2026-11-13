import fs from 'node:fs';
import path from 'node:path';
import {createHash,randomUUID} from 'node:crypto';
import {spawn} from 'node:child_process';

/** Browser-only derivative of the archived MPEG-4 Part 2 videos. Sources stay intact. */
export class Phase1Preview {
  private readonly pending=new Map<string,Promise<string>>();
  constructor(private readonly cacheRoot:string){}
  async file(source:string):Promise<string>{
    const stat=await fs.promises.stat(source);
    const key=createHash('sha256').update(JSON.stringify([fs.realpathSync(source),stat.size,stat.mtimeMs,'h264-v1'])).digest('hex');
    const target=path.join(this.cacheRoot,key+'.mp4');
    try{if((await fs.promises.stat(target)).size>0)return target;}catch{}
    let pending=this.pending.get(key);
    if(!pending){pending=this.convert(source,target).finally(()=>this.pending.delete(key));this.pending.set(key,pending);}
    return pending;
  }
  private async convert(source:string,target:string){
    await fs.promises.mkdir(this.cacheRoot,{recursive:true});
    const temporary=target+'.'+randomUUID()+'.tmp.mp4';
    try{
      await new Promise<void>((resolve,reject)=>{
        const child=spawn('ffmpeg',['-nostdin','-hide_banner','-loglevel','error','-threads','2','-i',source,'-map','0:v:0','-an','-c:v','libx264','-threads','2','-preset','fast','-crf','23','-pix_fmt','yuv420p','-movflags','+faststart',temporary],{shell:false,stdio:'ignore'});
        const timer=setTimeout(()=>{child.kill('SIGKILL');reject(new Error('PREVIEW_CONVERSION_TIMEOUT'));},90000);
        child.once('error',()=>{clearTimeout(timer);reject(new Error('PREVIEW_CONVERSION_FAILED'));});
        child.once('exit',code=>{clearTimeout(timer);if(code===0)resolve();else reject(new Error('PREVIEW_CONVERSION_FAILED'));});
      });
      await fs.promises.rename(temporary,target);return target;
    }finally{await fs.promises.rm(temporary,{force:true});}
  }
}
