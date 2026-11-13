import {readFileSync,readdirSync} from 'node:fs';
import path from 'node:path';
import {Ajv,type ValidateFunction} from 'ajv';

export type JsonObject=Record<string,unknown>;
export interface ToolDefinition {
  name:string;method:string;input:string;output:string;side_effect:boolean;purposes:string[];schema_file:string;
}
export interface WireError {
  code:string;stage:string;retryable:boolean;user_action:string;retained:string[];details_ref:string|null;simulation:boolean;
}
export interface RequestEnvelope {
  contract_version:'1.1.0';request_id:string;operation_id:string|null;method:string;params:JsonObject;simulation:boolean;
}
export interface ResponseEnvelope {
  contract_version:'1.1.0';request_id:string;ok:boolean;result:unknown;error:WireError|null;simulation:boolean;
}
export class ContractError extends Error {
  constructor(public readonly code:string,public readonly pointers:readonly string[]=[]){super(code);this.name='ContractError';}
}
interface SchemaDocument {
  $id:string;definitions:Record<string,unknown>;'x-tools'?:Omit<ToolDefinition,'schema_file'>[];
}

export class ContractRegistry {
  readonly tools:readonly ToolDefinition[];
  private readonly ajv=new Ajv({strict:false,allErrors:true});
  constructor(directory:string){
    const documents=readdirSync(directory).filter(f=>f.endsWith('.schema.json')).sort().map(file=>JSON.parse(readFileSync(path.join(directory,file),'utf8')) as SchemaDocument);
    for(const doc of documents)this.ajv.addSchema(doc);
    this.tools=Object.freeze(documents.flatMap(doc=>(doc['x-tools']||[]).map(t=>({...t,schema_file:path.basename(doc.$id,'.schema.json')}))));
    if(this.tools.length!==17||new Set(this.tools.map(t=>t.method)).size!==17||new Set(this.tools.map(t=>t.name)).size!==17)throw new ContractError('CONTRACT_REGISTRY_INVALID');
    for(const t of this.tools){this.checker(this.toolRef(t,'input'));this.checker(this.toolRef(t,'output'));}
  }
  tool(name:string):ToolDefinition {
    const tool=this.tools.find(t=>t.name===name||t.method===name);
    if(!tool)throw new ContractError('TOOL_UNKNOWN');return tool;
  }
  reference(file:string,definition:string):string{return `https://tennis.local/contracts/v1/${file}.schema.json#/definitions/${definition}`;}
  toolRef(tool:ToolDefinition,key:'input'|'output'):string {
    const value=tool[key];const [file,definition]=value.includes(':')?value.split(':'):[tool.schema_file,value];
    if(!file||!definition)throw new ContractError('CONTRACT_REGISTRY_INVALID');return this.reference(file,definition);
  }
  checker(reference:string):ValidateFunction {
    const validate=this.ajv.getSchema(reference);if(!validate)throw new ContractError('CONTRACT_DEFINITION_MISSING');return validate;
  }
  validate(file:string,definition:string,value:unknown):void {this.assert(this.reference(file,definition),value);}
  input(name:string,value:unknown):void {this.assert(this.toolRef(this.tool(name),'input'),value);}
  output(name:string,value:unknown):void {this.assert(this.toolRef(this.tool(name),'output'),value);}
  request(value:unknown):RequestEnvelope {
    this.validate('common','request',value);const request=value as RequestEnvelope;const t=this.tool(request.method);this.input(t.name,request.params);
    if(t.side_effect&&(!request.operation_id||request.operation_id!==request.params.operation_id))throw new ContractError('OPERATION_ID_MISMATCH');
    return request;
  }
  response(name:string,value:unknown):ResponseEnvelope {
    this.validate('common','response',value);const response=value as ResponseEnvelope;
    if(response.ok){this.output(name,response.result);const result=response.result as {simulation?:boolean};if(result.simulation!==response.simulation)throw new ContractError('SIMULATION_MISMATCH');}
    else if(response.error?.simulation!==response.simulation)throw new ContractError('SIMULATION_MISMATCH');
    return response;
  }
  private assert(reference:string,value:unknown):void {
    const checker=this.checker(reference);
    if(!checker(value))throw new ContractError('CONTRACT_INVALID',(checker.errors||[]).map(e=>e.instancePath||'/'));
  }
}
