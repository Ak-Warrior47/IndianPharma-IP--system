// Human-style click-through, run before every deploy (see CLAUDE.md).
// Signs in as each role on a phone-size screen, taps the real buttons and
// reports JS errors, failed requests and sideways scrolling.
//   1. python3.13 tools_click_through_seed.py      (local SQLite test accounts)
//   2. start the local server on port 5077
//   3. node tools_click_through.js                  (exit code 1 on any problem)
const {chromium,devices}=require('playwright'); const B='http://127.0.0.1:5077';
(async()=>{const b=await chromium.launch(); const out={}; const errs=[]; const bad=[];
 async function as(email,dev){const c=await b.newContext({...devices[dev||'Pixel 7']});const p=await c.newPage();
   p.on('pageerror',e=>errs.push(email+': '+e.message));
   p.on('response',r=>{ if(r.status()>=400 && r.url().indexOf(B)===0) bad.push(email+' '+r.status()+' '+r.url().replace(B,'')); });
   await p.goto(B+'/login');await p.fill('#email',email);await p.fill('#password','longpass123');await p.press('#password','Enter');await p.waitForLoadState('networkidle');return p;}
 const sq=s=>(s||'').split(/\s+/).join(' ').trim();
 const hs=p=>p.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
 // ---------- assigner
 const a=await as('asg@x.com'); await a.waitForTimeout(1200); const A={}; out.assigner=A;
 await a.click('#notif-bell'); await a.waitForTimeout(700);
 A.bellOnTop=await a.evaluate(()=>{const r=document.getElementById('notif-dropdown').getBoundingClientRect();const el=document.elementFromPoint(r.left+20,r.top+20);return r.height>30&&!!(el&&el.closest('#notif-dropdown'));});
 await a.screenshot({path:require('os').tmpdir()+'/ip_qa_bell.png'});
 await a.mouse.click(200,700); await a.waitForTimeout(200); A.bellClosesOutside=!(await a.isVisible('#notif-dropdown'));
 // repeat bill number: Enter must not move to quantity
 await a.fill('#pkBillNo','66201'); await a.press('#pkBillNo','Enter'); await a.waitForTimeout(700);
 A.dupFocus=await a.evaluate(()=>document.activeElement.id); A.dupMsg=sq(await a.textContent('#pkMsg'));
 await a.click('#pkBillItems'); await a.waitForTimeout(200); A.dupTapQtyFocus=await a.evaluate(()=>document.activeElement.id);
 await a.fill('#pkBillNo','66401'); await a.press('#pkBillNo','Enter'); await a.waitForTimeout(700);
 A.newFocus=await a.evaluate(()=>document.activeElement.id);
 await a.fill('#pkBillItems','5'); await a.press('#pkBillItems','Enter'); await a.waitForTimeout(1200);
 A.added=sq(await a.textContent('#pkAdded'));
 A.qtyPlaceholder=await a.getAttribute('#pkBillItems','placeholder');
 A.qtyFits=await a.evaluate(()=>{const i=document.getElementById('pkBillItems');const c=document.createElement('canvas').getContext('2d');c.font=getComputedStyle(i).font;return c.measureText('Quantity').width < i.clientWidth-16;});
 A.freeChip=await a.evaluate(()=>{const c=document.querySelector('.pk-chip.free');return c?getComputedStyle(c).backgroundColor:'';});
 for (const t of ['late','urgent','done','board','sched','rules','live']) { await a.click('.pk-tabs [data-tab="'+t+'"]'); await a.waitForTimeout(250); }
 await a.click('#pkToday [data-go-tab="done"]'); await a.waitForTimeout(200); A.tileCompleted=await a.getAttribute('.pk-tabs [data-tab="done"]','aria-selected');
 await a.click('.pk-tabs [data-tab="live"]'); A.hscroll=await hs(a);
 // ---------- picker
 const l=await as('lisa@x.com'); await l.waitForTimeout(1200); const L={}; out.picker=L;
 await l.click('#notif-bell'); await l.waitForTimeout(600);
 L.bellOnTop=await l.evaluate(()=>{const r=document.getElementById('notif-dropdown').getBoundingClientRect();const el=document.elementFromPoint(r.left+20,r.top+20);return !!(el&&el.closest('#notif-dropdown'));});
 await l.mouse.click(200,900); await l.waitForTimeout(200);
 await l.click('.pk-stat[data-go-tab="mine"]'); await l.waitForTimeout(200); L.tileMyDay=await l.getAttribute('.pk-tabs [data-tab="mine"]','aria-selected');
 for (const t of ['board','rules','live']) { await l.click('.pk-tabs [data-tab="'+t+'"]'); await l.waitForTimeout(250); }
 const id=await l.evaluate(()=>{const x=[...document.querySelectorAll('[data-take]')].find(x=>x.closest('.pk-bill').textContent.indexOf('66401')>=0);return x&&x.dataset.take;});
 await l.fill('#take'+id,'3'); await l.click('[data-take="'+id+'"]'); await l.waitForTimeout(1000);
 await l.click('#pkRepBtn'); await l.waitForTimeout(200);
 L.binIcon=await l.evaluate(()=>!!document.querySelector('#pkRepRows [data-rep-del] svg'));
 await (await l.$$('#pkRepRows input'))[0].fill('Volini gel');          // no quantity typed
 await l.click('#pkRepSend'); await l.waitForTimeout(400);
 L.needsRemark=sq(await l.textContent('#pkMsg')).indexOf('remark')>=0 && await l.evaluate(()=>document.body.classList.contains('pk-full'));
 await l.fill('#pkRepRows .rm','rack empty');
 await l.click('#pkRepSend'); await l.waitForTimeout(1500);
 L.msg=sq(await l.textContent('#pkMsg')); L.myMiss=sq(await l.textContent('#pkMyMiss')).slice(0,70);
 L.myMissBin=await l.evaluate(()=>!!document.querySelector('#pkMyMiss [data-miss-del] svg')); L.hscroll=await hs(l);
 await (await l.$('#pkMyMiss')).screenshot({path:require('os').tmpdir()+'/ip_qa_mymiss.png'});
 // delete the report, then take the item back with one tap
 await l.click('#pkMyMiss [data-miss-del]'); await l.waitForTimeout(1500);
 L.redoBanner=sq(await l.textContent('#pkRedo')).slice(0,60);
 await l.click('#pkRedo [data-redo]'); await l.waitForTimeout(1200);
 L.redoStarted=await l.evaluate(()=>document.body.classList.contains('pk-full'));
 await l.click('#pkRepBtn'); await l.waitForTimeout(200);
 await (await l.$$('#pkRepRows input'))[0].fill('Volini gel 30g'); await l.fill('#pkRepRows .rm','not in rack B4');
 await l.click('#pkRepSend'); await l.waitForTimeout(1500);
 // ---------- purchaser
 const p=await as('pur@x.com'); await p.waitForTimeout(1500); const P={}; out.purchaser=P;
 P.list=sq(await p.textContent('#pmList')).slice(0,110);
 P.redoNote=sq(await p.textContent('#pmRedo')).slice(0,60);
 await p.click('#pmList [data-pm="done"]'); await p.waitForTimeout(400);
 P.doneNeedsRemark=sq(await p.textContent('#pmMsg')).indexOf('remark')>=0 && (await p.$$('#pmList .pm-row')).length===1;
 await p.fill('#pmList .pm-rem input','Ordered, comes tomorrow'); await p.click('#pmList [data-pm="remark"]'); await p.waitForTimeout(900);
 P.remarkMsg=sq(await p.textContent('#pmMsg')); P.remarkKept=await p.inputValue('#pmList .pm-rem input');
 await (await p.$('#purMissing')).screenshot({path:require('os').tmpdir()+'/ip_qa_pur.png'});
 await p.click('#pmList [data-pm="done"]'); await p.waitForTimeout(900); P.after=sq(await p.textContent('#pmList')); P.hscroll=await hs(p);
 // ---------- admin
 const d=await as('adm@x.com'); const D={}; out.admin=D;
 await d.evaluate(()=>{try{localStorage.clear()}catch(e){}}); await d.goto(B+'/admin_dashboard#staff'); await d.waitForLoadState('networkidle');
 D.groups=await d.$$eval('#quick .qa-h',x=>x.map(e=>e.textContent)); D.buttons=await d.$$eval('#quick .qa-btn b',x=>x.map(e=>e.textContent));
 await d.screenshot({path:require('os').tmpdir()+'/ip_qa_admin.png'});
 D.links={}; for (const h of await d.$$eval('#quick a.qa-btn',x=>x.map(e=>e.getAttribute('href')).filter(h=>h[0]==='/'))) { D.links[h]=(await d.request.get(B+h.split('#')[0])).status(); }
 await d.click('.tab[data-tab="today"]'); await d.waitForTimeout(300);
 await d.click('.pt-tile:has-text("Missing")'); await d.waitForLoadState('networkidle');
 D.missingTile=d.url().replace(B,''); const mt=sq(await d.textContent('#miList'));
 D.remarkInReport=mt.indexOf('Ordered, comes tomorrow')>=0; D.pickerRemark=mt.indexOf('not in rack B4')>=0; D.finishedBy=mt.indexOf('Deepak')>=0;
 await d.click('#miTiles a:has-text("Deleted by picker")'); await d.waitForLoadState('networkidle'); D.deletedFilter=sq(await d.textContent('#miList h2'));
 await d.screenshot({path:require('os').tmpdir()+'/ip_qa_missing.png',fullPage:true});
 await d.click('#miList .fd-btn'); D.foldWorks=await d.evaluate(()=>document.getElementById('miList').classList.contains('is-fold'));
 await d.click('#miList .fd-btn');
 await d.goto(B+'/picking?tab=reports&period=today'); await d.waitForLoadState('networkidle'); D.pickingFoldButtons=await d.$$eval('.pa-box .fd-btn',x=>x.length);
 await d.click('#paSum a:has-text("Urgent bills")'); await d.waitForLoadState('networkidle'); D.urgentTile=sq(await d.textContent('#reports h2')).slice(0,40);
 await d.goto(B+'/admin/staff-today'); await d.click('.st-sum a:has-text("Not found")'); await d.waitForLoadState('networkidle'); D.staffTodayTile=d.url().replace(B,'').slice(0,60);
 D.hscroll=await hs(d);
 // ---------- checker
 // ---------- admin on a PC: buttons in a row, table buttons on one line
 const pc=await as('adm@x.com','Desktop Chrome'); await pc.goto(B+'/admin_dashboard#staff'); await pc.waitForLoadState('networkidle');
 out.adminPC={rows:await pc.evaluate(()=>[...new Set([...document.querySelectorAll('#quick .qa-btn')].map(e=>Math.round(e.getBoundingClientRect().top)))].length),
   actionsOneLine:await pc.evaluate(()=>{const a=document.querySelector('#staffTable .actions');const t=[...a.querySelectorAll('.btn-sm')].map(e=>Math.round(e.getBoundingClientRect().top));return Math.max(...t)-Math.min(...t)<6;}),
   hscroll:await hs(pc)};
 await pc.screenshot({path:require('os').tmpdir()+'/ip_qa_pc.png'});
 const c=await as('chk@x.com'); await c.waitForTimeout(600); out.checker={findBar:await c.isVisible('#bfQ'),hscroll:await hs(c)};
 console.log(JSON.stringify(out,null,1)); console.log('JS errors:',errs); console.log('Failed requests:',bad);
 const flat=JSON.stringify(out); const wrong=errs.length||bad.length||/"hscroll":true/.test(flat)||/"bellOnTop":false/.test(flat);
 console.log(wrong?'CLICK-THROUGH: PROBLEMS FOUND':'CLICK-THROUGH: ALL GOOD');
 await b.close(); process.exit(wrong?1:0);})();
