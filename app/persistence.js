/* Durable task outbox. No invitation/admin credentials are stored here. */
(() => {
 'use strict';
 const DB_NAME='barsan-task-outbox-v1';
 let databasePromise;
 const storageError=()=>Object.assign(new Error('ذخیره یا بازیابی پاسخ روی این دستگاه ممکن نشد. لطفاً صفحه را باز نگه دارید و با مسئول پژوهش تماس بگیرید.'),{localStorageFailure:true});
 function open(){
  if(!databasePromise)databasePromise=new Promise((resolve,reject)=>{
   if(!globalThis.indexedDB){reject(storageError());return;}
   const request=indexedDB.open(DB_NAME,1);
   let settled=false;
   const timer=setTimeout(()=>{settled=true;reject(storageError());},8000);
   request.onupgradeneeded=()=>{
    const store=request.result.createObjectStore('entries',{keyPath:'id'});
    store.createIndex('scope','scope',{unique:false});
   };
   request.onerror=()=>{clearTimeout(timer);reject(storageError());};
   request.onblocked=()=>{clearTimeout(timer);settled=true;reject(storageError());};
   request.onsuccess=()=>{
    clearTimeout(timer);
    if(settled){request.result.close();return;}
    const db=request.result;
    db.onversionchange=()=>{db.close();databasePromise=null;};
    resolve(db);
   };
  }).catch(error=>{databasePromise=null;throw error;});
  return databasePromise;
 }
 async function transaction(mode,work){
  const db=await open();
  return new Promise((resolve,reject)=>{
   let tx;
   try{tx=db.transaction('entries',mode);}catch(error){reject(storageError());return;}
   let result;
   const timer=setTimeout(()=>{try{tx.abort();}catch(error){}reject(storageError());},15000);
   tx.oncomplete=()=>{clearTimeout(timer);resolve(result);};
   tx.onabort=tx.onerror=()=>{clearTimeout(timer);reject(storageError());};
   try{work(tx.objectStore('entries'),value=>{result=value;},tx);}
   catch(error){tx.abort();}
  });
 }
 const scope=(...parts)=>JSON.stringify(parts);
 const id=(scope,key)=>JSON.stringify([scope,key]);
 function mergeValue(previous,next,mode){
  if(!previous)return structuredClone(next);
  if(mode==='immutable')return previous;
  if(mode==='events')return {...previous,...next,payload:{...previous.payload,...next.payload,
   event_timestamps:{...next.payload.event_timestamps,...previous.payload.event_timestamps},
   ...(previous.payload.wait_duration_ms===undefined?{}:{wait_duration_ms:previous.payload.wait_duration_ms})}};
  return structuredClone(next);
 }
 async function put(scope,key,value,mode='immutable'){
  return transaction('readwrite',(store,done)=>{
   const request=store.get(id(scope,key));
   request.onsuccess=()=>{
    const previous=request.result;
    const merged=mergeValue(previous?.value,value,mode);
    const entry={id:id(scope,key),scope,key,revision:(previous?.revision||0)+1,
     created_at:previous?.created_at||new Date().toISOString(),value:merged};
    store.put(entry);done(entry);
   };
  });
 }
 async function list(scopeValue){
  return transaction('readonly',(store,done)=>{
   const request=scopeValue===undefined?store.getAll():store.index('scope').getAll(scopeValue);
   request.onsuccess=()=>done(request.result.sort((a,b)=>a.created_at.localeCompare(b.created_at)||a.id.localeCompare(b.id)));
  });
 }
 async function ack(entries){
  return transaction('readwrite',(store,done)=>{
   for(const entry of entries){
    const request=store.get(entry.id);
    request.onsuccess=()=>{if(request.result?.revision===entry.revision)store.delete(entry.id);};
   }
   done(true);
  });
 }
 async function migrate(key,scopeValue,convert){
  // Re-check the exact legacy string before removal: another tab may append to it.
  const raw=localStorage.getItem(key);
  if(raw===null)return;
  let values;
  try{values=JSON.parse(raw);if(!Array.isArray(values))throw storageError();}
  catch(error){throw storageError();}
  const items=values.map(convert);
  if(items.some(item=>!item||!item.key||!item.value))throw storageError();
  await transaction('readwrite',(store,done)=>{
   for(const item of items){
    const entryId=id(scopeValue,item.key),request=store.get(entryId);
    request.onsuccess=()=>{
     const previous=request.result;
     store.put({id:entryId,scope:scopeValue,key:item.key,revision:(previous?.revision||0)+1,
      created_at:previous?.created_at||new Date().toISOString(),
      value:mergeValue(previous?.value,item.value,item.mode||'immutable')});
    };
   }
   done(true);
  });
  if(localStorage.getItem(key)===raw)localStorage.removeItem(key);
 }
 async function initialize(){
  await open();
  // Verify a committed write/read/delete before the first research response.
  const testScope=scope('probe',crypto.randomUUID());
  const entry=await put(testScope,'probe',{ok:true});
  const read=await list(testScope);
  if(read.length!==1||!read[0].value.ok)throw storageError();
  await ack([entry]);
  if(navigator.storage?.persist)navigator.storage.persist().catch(()=>{});
 }
 const acquired=new Map();
 async function acquire(participant){
  const name='barsan-task-'+participant;
  if(acquired.has(name)||!navigator.locks)return;
  await new Promise((resolve,reject)=>{
   navigator.locks.request(name,{ifAvailable:true},async lock=>{
    if(!lock){reject(new Error('این آزمایش در برگهٔ دیگری باز است. لطفاً همان برگه را ادامه دهید.'));return;}
    await new Promise(release=>{acquired.set(name,release);resolve();});
    acquired.delete(name);
   }).catch(reject);
  });
 }
 function release(participant){acquired.get('barsan-task-'+participant)?.();}
 globalThis.BarsanStore=Object.freeze({scope,put,list,ack,migrate,initialize,acquire,release});

 // One foreground and one background lane; telemetry cannot hold up a decision.
 const lanes={foreground:{active:false,jobs:[]},background:{active:false,jobs:[]}};
 function run(work,priority='foreground'){
  const lane=priority==='foreground'?lanes.foreground:lanes.background;
  return new Promise((resolve,reject)=>{
   lane.jobs.push({work,resolve,reject,priority:priority==='answers'?0:1});
   lane.jobs.sort((a,b)=>a.priority-b.priority);
   pump(lane);
  });
 }
 async function pump(lane){
  if(lane.active||!lane.jobs.length)return;
  lane.active=true;
  const job=lane.jobs.shift();
  try{job.resolve(await job.work());}catch(error){job.reject(error);}
  finally{lane.active=false;pump(lane);}
 }
 globalThis.BarsanTransport=Object.freeze({run});
})();
