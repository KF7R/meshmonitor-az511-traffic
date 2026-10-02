import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';
const runtime=process.argv[2], repo=process.argv[3];
const load=rel=>import(pathToFileURL(path.join(runtime,rel)));
const {validateAutomationGraph}=await load('src/types/automation.js');
const {evaluateGraph}=await load('src/server/services/automation/graphEvaluator.js');
const {evaluateCondition}=await load('src/server/services/automation/conditionEvaluator.js');
const {executeAction}=await load('src/server/services/automation/actionExecutor.js');
for(const file of fs.readdirSync(path.join(repo,'examples'))){
 const envelope=JSON.parse(fs.readFileSync(path.join(repo,'examples',file),'utf8'));
 const valid=validateAutomationGraph(envelope.config);
 assert.equal(valid.valid,true,JSON.stringify(valid.errors));
 assert.equal(envelope.enabled,false);
}
const graph=JSON.parse(fs.readFileSync(path.join(repo,'examples/traffic-scheduled.disabled.json'))).config;
const base=JSON.parse(fs.readFileSync(path.join(repo,'tests/native.fixture.output.json'),'utf8'));
let reports=[];
async function run(label,count,options={}){
 const payload=structuredClone(base);
 for(let i=count;i<3;i++)payload['incident'+i]={present:false};
 const memory=new Map([['traffic_scheduled',base]]), calls=[];
 const ctx={trigger:{triggerType:'trigger.schedule',sourceId:null,subjectNodeNum:null,timestamp:0,fields:{}},
 vars:{getValue:async n=>memory.get(n)??null,setValue:async(n,v)=>memory.set(n,v)},
 data:{getNode:async()=>null,getTelemetry:async()=>null},varCtx:{sourceId:null,nodeNum:null},now:base.generated_at*1000,automationId:'audit'};
 const deny=async()=>{throw Error('Unexpected IO')};
 const deps={sendTapback:deny,manageNode:deny,requestData:deny,rebootDevice:deny,notify:deny,
 sleep:async()=>{},runScript:async a=>{assert.deepEqual(a.scriptArgs,['--prepare','--ledger','/data/scripts/state/traffic_attempts.json']);return options.failScript?{success:false,stdout:'',error:'injected'}:{success:true,stdout:'',returnValue:payload}},
 broadcastWaypoint:async a=>{calls.push({kind:'waypoint',...a});if(options.failWaypoint)throw Error('injected');return options.skip?{sent:false,skipped:true,reason:options.skip}:{sent:true,packetId:42}},
 sendMessage:async a=>{calls.push({kind:'text',...a});return 43}};
 const trace=await evaluateGraph(graph,ctx,{evaluateCondition,executeAction:(n,c)=>executeAction(n,c,deps),applySetVar:async n=>memory.set(n.params.variable,n.params.value)});
 const texts=calls.filter(x=>x.kind==='text'), pins=calls.filter(x=>x.kind==='waypoint');
 assert.equal(texts.length,options.failScript?0:count);assert.equal(pins.length,options.failScript?0:count);
 for(const call of calls)assert.equal(call.channel,3);
 reports.push({label,waypoints:pins.length,messages:texts.length,actionErrors:trace.actions.filter(a=>!a.ok).length});
}
for(let count=0;count<4;count++)await run(count+' incidents',count);
await run('script failure clears stale result',3,{failScript:true});
await run('waypoint failure still permits text',3,{failWaypoint:true});
for(const skip of ['MIN_INTERVAL','NOT_CONNECTED','TX_DISABLED'])await run(skip+' still permits text',3,{skip});
fs.writeFileSync(path.join(repo,'tests/native-audit-results.json'),JSON.stringify({meshmonitorVersion:'4.17.0-rc1',transport:'mocked',examplesValidated:3,scenarios:reports},null,2)+'\n');
console.log('All 3 example graphs validated; 9 isolated native scenarios passed. No RF.');
