import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {ExpertError} from './expert-policy.js';
export const DEEPSEEK_BASE='https://api.deepseek.com';
export const DEFAULT_MODEL='deepseek-flash';
export interface DeepSeekConfig {api_key:string;model:string;}
export type ChatContent={type:'text';text:string}|{type:'image_url';image_url:{url:string}};
export interface ChatMessage {role:'system'|'user'|'assistant';content:string|ChatContent[];}
type Fetch=typeof fetch;
export class DeepSeekClient {
  constructor(private readonly configPath=process.env.TENNIS_MODEL_CONFIG||path.join(os.homedir(),'.config/tennis-coach/deepseek.json'),private readonly transport:Fetch=fetch,private readonly environment:NodeJS.ProcessEnv=process.env){}
  private config():DeepSeekConfig {
    let value:Record<string,unknown>={};
    if(fs.existsSync(this.configPath)){
      const stat=fs.lstatSync(this.configPath);
      if(!stat.isFile()||stat.isSymbolicLink()||stat.size>8192||(process.platform!=='win32'&&(stat.mode&0o077)!==0))throw new ExpertError('MODEL_CONFIG_PERMISSIONS');
      try{value=JSON.parse(fs.readFileSync(this.configPath,'utf8'));}catch{throw new ExpertError('MODEL_CONFIG_INVALID');}
      if(!value||typeof value!=='object'||Array.isArray(value))throw new ExpertError('MODEL_CONFIG_INVALID');
    }
    const api_key=this.environment.DEEPSEEK_API_KEY||value.api_key,model=this.environment.TENNIS_DEEPSEEK_MODEL||value.model||DEFAULT_MODEL;
    if(typeof api_key!=='string'||!api_key.trim())throw new ExpertError('MODEL_KEY_MISSING',503);
    if(api_key.length>512||/[\r\n]/.test(api_key)||typeof model!=='string'||!/^deepseek-[a-z0-9.-]{1,64}$/.test(model))throw new ExpertError('MODEL_CONFIG_INVALID');
    return {api_key:api_key.trim(),model};
  }
  status(){try{return {configured:true,provider:'DeepSeek 官方 API',model:this.config().model,base_url:DEEPSEEK_BASE};}catch(e){return {configured:false,provider:'DeepSeek 官方 API',model:DEFAULT_MODEL,base_url:DEEPSEEK_BASE,error:e instanceof ExpertError?e.code:'MODEL_CONFIG_INVALID'};}}
  private async request(endpoint:string,init:RequestInit,signal?:AbortSignal):Promise<Record<string,unknown>>{
    const config=this.config();
    let response:Response;
    try{response=await this.transport(DEEPSEEK_BASE+endpoint,{...init,redirect:'error',headers:{Authorization:'Bearer '+config.api_key,'Content-Type':'application/json'},signal:AbortSignal.any([AbortSignal.timeout(90000),...(signal?[signal]:[])])});}
    catch{throw new ExpertError(signal?.aborted?'EXPERT_CANCELLED':'MODEL_CONNECTION_FAILED',502);}
    if(!response.ok){await response.body?.cancel();throw new ExpertError(response.status===401?'MODEL_AUTH_FAILED':response.status===429?'MODEL_RATE_LIMITED':'MODEL_HTTP_ERROR',502);}
    let raw='',size=0;const reader=response.body?.getReader(),decoder=new TextDecoder();if(!reader)throw new ExpertError('MODEL_RESPONSE_INVALID',502);
    try{while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>1024*1024){await reader.cancel();throw new Error();}raw+=decoder.decode(value,{stream:true});}raw+=decoder.decode();const result:unknown=JSON.parse(raw);if(!result||typeof result!=='object'||Array.isArray(result))throw new Error();return result as Record<string,unknown>;}catch{throw new ExpertError('MODEL_RESPONSE_INVALID',502);}
  }
  async models(signal?:AbortSignal){const value=await this.request('/models',{method:'GET'},signal);const data=value.data;if(!Array.isArray(data))throw new ExpertError('MODEL_RESPONSE_INVALID',502);return {provider:'DeepSeek 官方 API',models:data.flatMap(x=>x&&typeof x.id==='string'?[x.id]:[]),selected:this.config().model};}
  async chat(messages:ChatMessage[],signal?:AbortSignal){
    let images=0,total=0;for(const message of messages)if(Array.isArray(message.content))for(const part of message.content)if(part.type==='image_url'){
      images++;const url=part.image_url.url;if(!/^data:image\/png;base64,[A-Za-z0-9+/]+=*$/.test(url))throw new ExpertError('MODEL_IMAGE_INPUT_DENIED',403);total+=url.length;
    }
    if(images>3||total>17*1024*1024)throw new ExpertError('MODEL_IMAGE_INPUT_TOO_LARGE',413);
    if(images&&this.config().model!=='deepseek-flash')throw new ExpertError('MODEL_IMAGE_UNSUPPORTED',409);
    const value=await this.request('/chat/completions',{method:'POST',body:JSON.stringify({model:this.config().model,messages,thinking:{type:'disabled'},response_format:{type:'json_object'},max_tokens:4096,stream:false})},signal);
    const choice=(value.choices as {finish_reason?:string;message?:{content?:unknown}}[]|undefined)?.[0];
    if(choice?.finish_reason==='length')throw new ExpertError('MODEL_OUTPUT_TRUNCATED',502);
    if(choice?.finish_reason&&choice.finish_reason!=='stop')throw new ExpertError('MODEL_RESPONSE_INCOMPLETE',502);
    const content=choice?.message?.content;
    if(typeof content!=='string')throw new ExpertError('MODEL_RESPONSE_INVALID',502);
    if(!content.trim())throw new ExpertError('MODEL_OUTPUT_EMPTY',502);
    let result:unknown;try{result=JSON.parse(content);}catch{throw new ExpertError('MODEL_JSON_INVALID',502);}
    return {result,usage:value.usage&&typeof value.usage==='object'?value.usage:null};
  }
}
