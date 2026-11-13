import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
export interface SourceCard {source_id:string;title:string;url:string;limits:string;}
export interface ReadSource extends SourceCard {status:'read'|'unavailable';excerpt:string;checked_at:string;fingerprint:string|null;}
const PREFIX='https://www.lta.org.uk/play/tennis-tips-and-techniques/';
export class ExpertSources {
  constructor(private readonly repoRoot:string,private readonly transport:typeof fetch=fetch){}
  select(question:string):SourceCard[]{
    const value=JSON.parse(fs.readFileSync(path.join(this.repoRoot,'workbuddy-plugin/skills/tennis-evidence-reasoning/references/sources.json'),'utf8'));
    const ids=new Set<string>();
    if(/反手|backhand/i.test(question))ids.add('SRC-LTA-BH');
    if(/发球|serve/i.test(question))ids.add('SRC-LTA-SERVE');
    if(/截击|volley/i.test(question))ids.add('SRC-LTA-VOLLEY');
    if(/正手|forehand/i.test(question))ids.add('SRC-LTA-FH');
    if(/步伐|移动|footwork|拉拍|准备/i.test(question))ids.add('SRC-LTA-FOOT');
    if(!ids.size){ids.add('SRC-LTA-FH');ids.add('SRC-LTA-FOOT');}
    return (value.sources as SourceCard[]).filter(s=>ids.has(s.source_id)&&s.url.startsWith(PREFIX)&&/^https:\/\/www\.lta\.org\.uk\/play\/tennis-tips-and-techniques\/[a-z-]+\/$/.test(s.url)).slice(0,3).map(({source_id,title,url,limits})=>({source_id,title,url,limits}));
  }
  async read(cards:SourceCard[],signal?:AbortSignal):Promise<ReadSource[]>{
    return Promise.all(cards.map(async card=>{
      const base={...card,checked_at:new Date().toISOString()};
      if(!card.url.startsWith(PREFIX))return {...base,status:'unavailable' as const,excerpt:'',fingerprint:null};
      try{
        const response=await this.transport(card.url,{redirect:'error',signal:AbortSignal.any([AbortSignal.timeout(12000),...(signal?[signal]:[])])});
        if(!response.ok||!response.headers.get('content-type')?.includes('text/html')){await response.body?.cancel();throw new Error();}
        const reader=response.body?.getReader();if(!reader)throw new Error();let bytes=0;const chunks:Uint8Array[]=[];
        while(true){const {value,done}=await reader.read();if(done)break;bytes+=value.byteLength;if(bytes>2*1024*1024){await reader.cancel();throw new Error();}chunks.push(value);}
        const raw=Buffer.concat(chunks).toString('utf8');const main=raw.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1]||raw;
        const text=main.replace(/<(script|style|nav|header|footer)\b[^>]*>[\s\S]*?<\/\1>/gi,' ').replace(/<[^>]+>/g,' ').replace(/&nbsp;|&#160;/g,' ').replace(/&amp;/g,'&').replace(/&(?:quot|#39);/g,'"').replace(/\s+/g,' ').trim();
        const at=text.toLowerCase().indexOf(card.title.toLowerCase());const excerpt=text.slice(Math.max(0,at),Math.max(0,at)+5000);
        if(excerpt.length<200)throw new Error();return {...base,status:'read' as const,excerpt,fingerprint:createHash('sha256').update(excerpt).digest('hex')};
      }catch{return {...base,status:'unavailable' as const,excerpt:'',fingerprint:null};}
    }));
  }
}
