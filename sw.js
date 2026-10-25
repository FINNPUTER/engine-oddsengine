/* OddsEngine app: offline shell and phone alerts. The API is never cached. */
const V='oe-app-v40';
const SHELL=['./','index.html','icon-192.png','icon-512.png','badge-96.png','robot-ring.webp','manifest.webmanifest'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(V).then(c=>c.addAll(SHELL)).catch(()=>{}));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener('fetch',e=>{
  const r=e.request;if(r.method!=='GET')return;
  const u=new URL(r.url);if(u.origin!==location.origin)return;
  if(r.mode==='navigate'){
    e.respondWith(fetch(r).then(res=>{const c=res.clone();caches.open(V).then(k=>k.put('index.html',c));return res;}).catch(()=>caches.match('index.html')));return;}
  e.respondWith(caches.match(r).then(hit=>{const net=fetch(r).then(res=>{if(res.ok){const c=res.clone();caches.open(V).then(k=>k.put(r,c));}return res;}).catch(()=>hit);return hit||net;}));
});
self.addEventListener('push',e=>{
  let d={};try{d=e.data?e.data.json():{};}catch(_){d={body:e.data?e.data.text():''};}
  e.waitUntil(self.registration.showNotification(d.title||'Your Engine',{body:d.body||'',icon:'icon-192.png',badge:'badge-96.png',tag:d.tag||undefined,renotify:!!d.tag,data:{url:d.url||'./#engine'}}));
});
self.addEventListener('notificationclick',e=>{
  e.notification.close();const url=new URL((e.notification.data||{}).url||'./#engine',self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(cs=>{
    for(const c of cs){if(c.url.startsWith(self.registration.scope)&&'focus' in c){c.navigate&&c.navigate(url).catch(()=>{});return c.focus();}}
    return self.clients.openWindow(url);}));
});
