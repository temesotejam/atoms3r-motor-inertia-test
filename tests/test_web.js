// Exercise the actual embedded-page script with an offline motor-status mock.
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync(require('path').join(__dirname,'../web/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes={};
for(const id of html.matchAll(/id="([^"]+)"/g)) nodes[id[1]]={value:'',checked:false,disabled:false,hidden:false,textContent:'',getContext(){return new Proxy({}, {get:()=>()=>{},set:()=>true})}};
Object.assign(nodes.mode,{value:'single'});Object.assign(nodes.duration,{value:'100'});
for(const [id,value] of Object.entries({label:'fixed_01',current:'100',repeats:'3',limit:'1500','voltage-limit':'6000',level1:'50',level2:'100',level3:'150'})) nodes[id].value=value;
let status={revision:'test',phase:'idle',reason:'ready',active:false,ready:true,pending:false,stop_confirmed:true,frozen:true,rpm:0,current_mA:0,voltage_mV:7400,temperature_C:25,rows:0,missed:0,i2c_errors:0,pulse:0,total:0,run:0},sent=[];
const context=vm.createContext({document:{getElementById:id=>nodes[id]},AbortSignal:{timeout(){}},URLSearchParams,console,confirm:()=>false,setTimeout(){},fetch:async(url,options)=>{
  if(url==='/status')return {ok:true,json:async()=>status};
  sent.push({url,body:options.body});return {ok:true,text:async()=>'accepted'};
}});
(async()=>{
  vm.runInContext(script,context);await new Promise(setImmediate);
  assert(nodes.start.disabled);nodes.fixed.checked=true;nodes.fixed.onchange();assert(!nodes.start.disabled);
  nodes.mode.value='response';nodes.mode.onchange();assert(nodes['single-area'].hidden);
  assert(!nodes['response-area'].hidden);assert(nodes.duration.disabled);assert(nodes.start.disabled);
  status={...status,position_valid:true,position_deg:12.3,motor_firmware:4};
  await vm.runInContext('update()',context);assert(!nodes.start.disabled);
  assert.equal(nodes.current.value,300);assert.equal(nodes.repeats.value,3);
  await nodes.start.onclick();assert.equal(sent.at(-1).body.get('mode'),'response');sent=[];
  nodes.mode.value='batch';nodes.mode.onchange();assert.equal(nodes.duration.value,400);assert(nodes['single-area'].hidden);assert(!nodes['batch-area'].hidden);
  await nodes.start.onclick();assert.equal(sent[0].url,'/start');assert.equal(sent[0].body.get('fixed'),'1');assert.equal(sent[0].body.get('mode'),'batch');assert.equal(sent[0].body.get('level3'),'150');
  status={...status,phase:'pulse',active:true,ready:false,rows:50,stop_confirmed:false,run:1};
  await vm.runInContext('update()',context);assert(nodes.settings.disabled);assert(nodes.download.disabled);assert(nodes.clear.disabled);
  await nodes.stop.onclick();assert.equal(sent.at(-1).url,'/stop');
  status={...status,phase:'done',active:false,ready:true,stop_confirmed:true,frozen:true};
  await vm.runInContext('update()',context);assert(!nodes.download.disabled);assert(!nodes.clear.disabled);assert(nodes.start.disabled);
  console.log('Web UI: fixed configuration, start, batch defaults, active interlocks, STOP and completed-log controls PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
