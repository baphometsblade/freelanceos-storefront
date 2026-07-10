const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const dirs=['','blog','checkout','templates','promptvault'];
let files=[];
for(const d of dirs){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;
for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
const counts={};function C(k,n){counts[k]=(counts[k]||0)+n;}
for(const f of files){let s=fs.readFileSync(f,'utf8');const o=s;let n=0;
const reps=[
[/4\.9\/5<\/strong> from 312 reviews/g,'30-day<\/strong> money-back guarantee'],
[/4\.9\/5 from 312 reviews/g,'30-day money-back guarantee'],
[/ from 312 reviews/g,''],[/312 reviews/g,'a money-back guarantee'],
[/4\.9\/5 rating/g,'30-day refund guarantee'],[/4\.9\/5 stars/g,'money-back guarantee'],
[/\(4\.9\/5\)/g,''],[/4\.9\/5/g,'New'],
[/baphometsblade\.github\.io\/freelanceos-storefront/g,'freelanceos-storefront.vercel.app'],
[/baphometsblade\.github\.io/g,'freelanceos-storefront.vercel.app']];
for(const[a,b]of reps){s=s.replace(a,m=>{n++;return b;});}
if(s!==o){fs.writeFileSync(f,s,'utf8');C('pass2',n);}}
console.log('pass2 replacements:',counts.pass2||0);
// verify clean
let g=0,r=0;for(const f of files){const s=fs.readFileSync(f,'utf8');
g+=(s.match(/baphometsblade\.github\.io/g)||[]).length;r+=(s.match(/4\.9\/5/g)||[]).length;}
console.log('remaining ghio:',g,'remaining 4.9/5:',r);
console.log('dirs at root:',fs.readdirSync(ROOT).filter(f=>fs.statSync(path.join(ROOT,f)).isDirectory()).join(', '));
