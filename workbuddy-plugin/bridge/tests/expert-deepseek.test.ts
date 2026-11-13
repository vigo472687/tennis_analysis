import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {DeepSeekClient} from '../src/expert-deepseek.js';
test('missing key is explicit; fixed official host; auth errors do not leak body or repeat',async()=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'expert-key-'));
  try{
    const config=path.join(dir,'key.json');let calls=0;
    const transport:typeof fetch=async(input,init)=>{calls++;assert.equal(String(input),'https://api.deepseek.com/models');assert.equal(init?.redirect,'error');return new Response('private-secret',{status:401});};
    const missing=new DeepSeekClient(config,transport,{});assert.equal(missing.status().configured,false);await assert.rejects(missing.models(),/MODEL_KEY_MISSING/);assert.equal(calls,0);
    fs.writeFileSync(config,JSON.stringify({api_key:'test-key'}),{mode:0o600});const client=new DeepSeekClient(config,transport,{});
    assert.equal(JSON.stringify(client.status()).includes('test-key'),false);await assert.rejects(client.models(),/MODEL_AUTH_FAILED/);assert.equal(calls,1);
    fs.chmodSync(config,0o644);if(process.platform!=='win32')assert.equal(client.status().error,'MODEL_CONFIG_PERMISSIONS');
  }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
test('structured chat, cancellation and malformed JSON remain distinct',async()=>{
  const client=new DeepSeekClient('/nonexistent',async(_input,init)=>{assert.match(String(init?.body),/json_object/);return Response.json({choices:[{message:{content:'{"answer":"test"}'}}],usage:{total_tokens:1}});},{DEEPSEEK_API_KEY:'test-key'});
  assert.deepEqual((await client.chat([{role:'user',content:'test'}])).result,{answer:'test'});
  const broken=new DeepSeekClient('/nonexistent',async()=>Response.json({choices:[{message:{content:'broken'}}]}),{DEEPSEEK_API_KEY:'test-key'});await assert.rejects(broken.chat([]),/MODEL_JSON_INVALID/);
});
test('Chinese JSON split across UTF-8 chunks retains its content',async()=>{
  const bytes=new TextEncoder().encode(JSON.stringify({choices:[{message:{content:JSON.stringify({answer:'准备动作'})}}]}));
  const client=new DeepSeekClient('/nonexistent',async()=>new Response(new ReadableStream({start(controller){for(let i=0;i<bytes.length;i++)controller.enqueue(bytes.slice(i,i+1));controller.close();}})),{DEEPSEEK_API_KEY:'test-key'});
  assert.deepEqual((await client.chat([])).result,{answer:'准备动作'});
});
test('structured calls explicitly disable default thinking so reasoning cannot consume the answer budget',async()=>{
  const observations:unknown[]=[];
  const client=new DeepSeekClient('/nonexistent',async(_input,init)=>{
    const request=JSON.parse(String(init?.body));observations.push(request.thinking);
    const answer=request.thinking?.type==='disabled'?{finish_reason:'stop',message:{content:'{"answer":"先从低速准备开始"}'}}:{finish_reason:'length',message:{content:'',reasoning_content:'reasoning only'}};
    return Response.json({choices:[answer],usage:{completion_tokens:4096}});
  },{DEEPSEEK_API_KEY:'test-key'});
  assert.deepEqual((await client.chat([{role:'user',content:'只返回JSON'}])).result,{answer:'先从低速准备开始'});
  assert.deepEqual(observations,[{type:'disabled'}]);
});
test('truncated and blank JSON are distinguished and never retried by the transport',async()=>{
  for(const [choice,code] of [
    [{finish_reason:'length',message:{content:''}},'MODEL_OUTPUT_TRUNCATED'],
    [{finish_reason:'length',message:{content:'{"answer":"partial"}'}},'MODEL_OUTPUT_TRUNCATED'],
    [{finish_reason:'stop',message:{content:'   \n'}},'MODEL_OUTPUT_EMPTY']
  ] as const){
    let calls=0;const client=new DeepSeekClient('/nonexistent',async()=>{calls++;return Response.json({choices:[choice]});},{DEEPSEEK_API_KEY:'test-key'});
    await assert.rejects(client.chat([]),new RegExp(code));assert.equal(calls,1);
  }
});
test('vision sends only explicitly supplied inline PNGs and refuses arbitrary URLs or unsupported models',async()=>{
 let calls=0;const transport:typeof fetch=async(_url,init)=>{calls++;const body=JSON.parse(String(init?.body));assert.equal(body.messages[0].content[1].image_url.url,'data:image/png;base64,YWJj');return Response.json({choices:[{finish_reason:'stop',message:{content:'{}'}}]});};
 const client=new DeepSeekClient('/nonexistent',transport,{DEEPSEEK_API_KEY:'test-key'});
 await client.chat([{role:'user',content:[{type:'text',text:'selected frame'},{type:'image_url',image_url:{url:'data:image/png;base64,YWJj'}}]}]);assert.equal(calls,1);
 await assert.rejects(client.chat([{role:'user',content:[{type:'image_url',image_url:{url:'https://private.example/image'}}]}]),/MODEL_IMAGE_INPUT_DENIED/);assert.equal(calls,1);
 const pro=new DeepSeekClient('/nonexistent',transport,{DEEPSEEK_API_KEY:'test-key',TENNIS_DEEPSEEK_MODEL:'deepseek-v4-pro'});await assert.rejects(pro.chat([{role:'user',content:[{type:'image_url',image_url:{url:'data:image/png;base64,YWJj'}}]}]),/MODEL_IMAGE_UNSUPPORTED/);assert.equal(calls,1);
});
