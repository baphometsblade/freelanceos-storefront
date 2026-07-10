const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const V='https://freelanceos-storefront.vercel.app';
const DYN="(function(){var n=new Date();return Date.UTC(n.getUTCFullYear(),n.getUTCMonth(),n.getUTCDate()+(7-n.getUTCDay()),23,59,59);})()";
const counts={};
function C(k,n){counts[k]=(counts[k]||0)+n;}
function rep(s,k,a,b){let n=0;if(a instanceof RegExp){s=s.replace(a,m=>{n++;return typeof b==='function'?b(m):b;});}else{let i;while((i=s.indexOf(a))>=0){s=s.slice(0,i)+b+s.slice(i+a.length);n++;}}
C(k,n);return s;}
function fixFile(f){
let s=fs.readFileSync(f,'utf8');const o=s;
// domains -> vercel
s=rep(s,'domain','https://baphometsblade.github.io/freelanceos-storefront/',V+'/');
s=rep(s,'domain','https://baphometsblade.github.io/freelanceos-storefront',V);
s=rep(s,'domain','https://baphometsblade.github.io/',V+'/');
// \9 price bug
s=rep(s,'slash9',/\\9(?=[ <&])/g,()=>'$9');
// mojibake
s=rep(s,'moji','â€”','—');s=rep(s,'moji','â€“','–');s=rep(s,'moji','â€™','’');
s=rep(s,'moji','â€œ','“');s=rep(s,'moji','â€�','”');
s=rep(s,'moji',"'Sendingï¿½'","'Sending…'");s=rep(s,'moji',' ï¿½ ',' · ');
s=rep(s,'moji','ï¿½ we reply','· we reply');s=rep(s,'moji','// 0=Sun ï¿½ 6=Sat','// 0=Sun to 6=Sat');
s=rep(s,'moji','>?? How buying works','>🛡️ How buying works');
s=rep(s,'moji','<span style="font-size:1.1rem;">??</span> Quick question?','<span style="font-size:1.1rem;">💬</span> Quick question?');
s=rep(s,'moji','? Try the  Quick Start ï¿½ 3 core databases','⚡ Try the Quick Start · 3 core databases');
// expired countdown deadlines -> rolling next-Sunday
s=rep(s,'deadline',/new Date\('2026-0[1-7]-\d{2}T00:00:00Z'\)\.getTime\(\)/g,()=>DYN);
s=rep(s,'deadline','expires June 1st','ends Sunday');
// fake activity toast: HTML block
s=rep(s,'toastHtml',/<!--[^>\n]*LIVE ACTIVITY TOAST[^>\n]*-->\s*<div class="activity-toast"[\s\S]*?<\/button>\s*<\/div>\s*/g,'');
// fake toast JS (IIFE containing visitKey)
{let i=s.indexOf('var visitKey');
 while(i>=0){let st=s.lastIndexOf('(function',i);let en=s.indexOf('})();',i);
  if(st>=0&&en>i){s=s.slice(0,st)+s.slice(en+5);C('toastJs',1);}else break;
  i=s.indexOf('var visitKey');}}
// fake scarcity "uses left"
s=rep(s,'usesLeft',/(&nbsp;\|&nbsp;\s*)?&#127903;&#65039; <span id="usesLeft"[^<]*<\/span> uses left (&mdash;|—)\s*/g,'');
s=rep(s,'usesLeft',/\| 🎟️ <span id="usesLeft"[^<]*<\/span> uses left —\s*/g,'');
// fabricated user counts / ratings
s=rep(s,'stats','&#128101; 10,000+ freelancers &nbsp;&middot;&nbsp; &#11088; 4.9/5 rated','&#128737;&#65039; 30-day refund &nbsp;&middot;&nbsp; &#9889; Instant delivery');
s=rep(s,'stats','👥 10,000+ freelancers · ⭐ 4.9/5 rated','🛡️ 30-day refund · ⚡ Instant delivery');
s=rep(s,'stats','👥 2,400+ builders · ⭐ 4.9/5 rated','🛡️ 30-day refund · ⚡ Instant delivery');
s=rep(s,'stats','&#128101; 2,400+ builders &nbsp;&middot;&nbsp; &#11088; 4.9/5 rated','&#128737;&#65039; 30-day refund &nbsp;&middot;&nbsp; &#9889; Instant delivery');
s=rep(s,'stats',/\s*47 people browsing now/g,'');
s=rep(s,'stats','⭐⭐⭐⭐⭐ Trusted by 3,400+ freelancers','Built by a working freelancer');
s=rep(s,'stats','<div class="stat"><div class="stat-num">10k+</div><div class="stat-label">Freelancers</div></div>','<div class="stat"><div class="stat-num">120+</div><div class="stat-label">Guides &amp; articles</div></div>');
s=rep(s,'stats','<div class="stat"><div class="stat-num">4.9/5</div><div class="stat-label">Average rating</div></div>','<div class="stat"><div class="stat-num">30-day</div><div class="stat-label">Refund guarantee</div></div>');
s=rep(s,'stats',/Used by [\d,]+\+ /g,'');s=rep(s,'stats',/used by [\d,]+\+ /g,'for ');
s=rep(s,'stats',/Trusted by [\d,]+\+ /g,'Built for ');s=rep(s,'stats',/Join [\d,]+\+ /g,'Join ');
s=rep(s,'stats','&#11088; 4.9/5 rated','&#128737;&#65039; 30-day refund');
s=rep(s,'stats','★★★★★ 4.9/5','30-day money-back guarantee');
// truthful email capture
s=rep(s,'capture','&#10003; Code sent! Check your email for <strong>FLASH40</strong>','&#10003; Your code: <strong>FLASH40</strong> — auto-applied when you click any checkout button');
s=rep(s,'capture','✓ Code sent! Check your email for <strong>FLASH40</strong>','✓ Your code: <strong>FLASH40</strong> — auto-applied when you click any checkout button');
s=rep(s,'capture',/We'll send you the <strong[^>]*>FLASH40<\/strong> discount code \+ a free Notion setup guide to get you started fast\./g,'Enter your email and your <strong style="color:#c4b5fd;">FLASH40</strong> code is revealed instantly — right here on this page.');
s=rep(s,'capture',' Code valid for 48 hours.','');
s=rep(s,'capture','Sent! Check your inbox for your rate results + template.','Done — your results are shown above. Screenshot to save them.');
s=rep(s,'capture','we reply to every message within 2 hours during business hours','we reply to every message as soon as we can');
s=rep(s,'capture','Typically replies within 2 hours','We read every message');
if(s!==o){fs.writeFileSync(f,s,'utf8');return true;}
return false;
}
// collect files
const dirs=['','blog','checkout','templates','promptvault'];
let files=[];
for(const d of dirs){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;
for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
let changed=0;
for(const f of files){try{if(fixFile(f))changed++;}catch(e){console.log('ERR',f,e.message);}}
console.log('files scanned:',files.length,'changed:',changed);
console.log(JSON.stringify(counts,null,1));
// leftover scans
let left={moji:0,slash9:0,ghio:0,rating:0,files:{}};
for(const f of files){const s=fs.readFileSync(f,'utf8');
const m=(s.match(/ï¿½|â€/g)||[]).length,n9=(s.match(/\\9/g)||[]).length,g=(s.match(/baphometsblade\.github\.io/g)||[]).length,r=(s.match(/4\.9\/5/g)||[]).length;
if(m+n9+g>0)left.files[path.basename(f)]=[m,n9,g,r];
left.moji+=m;left.slash9+=n9;left.ghio+=g;left.rating+=r;}
console.log('LEFTOVERS',JSON.stringify(left.files).slice(0,1500));
console.log('totals moji',left.moji,'slash9',left.slash9,'ghio',left.ghio,'4.9/5',left.rating);
