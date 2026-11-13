import test from 'node:test';import assert from 'node:assert/strict';import path from 'node:path';
import {ExpertSources} from '../src/expert-sources.js';
test('only catalogued official URLs are used; page failure is never verified evidence',async()=>{
  const sources=new ExpertSources(path.resolve('../..'),async()=>new Response('offline',{status:503}));
  const selected=sources.select('我正手准备和步伐有问题');assert.ok(selected.length>0);assert.ok(selected.every(s=>s.url.startsWith('https://www.lta.org.uk/')));
  const read=await sources.read(selected);assert.ok(read.every(s=>s.status==='unavailable'&&!s.excerpt&&!s.fingerprint));
});
test('source excerpt excludes scripts and carries a checked hash',async()=>{
  const sources=new ExpertSources(path.resolve('../..'),async()=>new Response('<main>How to hit a tennis forehand '+('practice movement '.repeat(40))+'<script>secret commands</script></main>',{headers:{'content-type':'text/html'}}));
  const [source]=await sources.read(sources.select('正手'));assert.equal(source?.status,'read');assert.ok(source?.fingerprint);assert.ok(!source?.excerpt.includes('secret commands'));
});
