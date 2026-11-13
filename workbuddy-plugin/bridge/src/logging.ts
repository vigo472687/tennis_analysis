export interface LogEvent {
  task_id?:string;operation_id?:string;stage:string;code:string;
  outcome:'success'|'failure'|'cancelled'|'pending';simulation:boolean;duration_ms?:number;
}
const stages=new Set(['runtime','input','media','analysis','render','export','delete']);
function identifier(value:unknown):string|null {
  return typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value)&&!/^sk-|^Bearer/i.test(value)?value:null;
}
export class SafeLogger {
  constructor(private readonly sink:(line:string)=>unknown=line=>process.stderr.write(line)){}
  log(event:LogEvent):void {
    const record={timestamp:new Date().toISOString(),task_id:identifier(event.task_id),operation_id:identifier(event.operation_id),stage:stages.has(event.stage)?event.stage:'runtime',code:/^[A-Z][A-Z0-9_]{0,79}$/.test(event.code)?event.code:'REDACTED',outcome:event.outcome,simulation:event.simulation,duration_ms:Number.isFinite(event.duration_ms)&&Number(event.duration_ms)>=0?event.duration_ms:null};
    this.sink(JSON.stringify(record)+'\n');
  }
}
