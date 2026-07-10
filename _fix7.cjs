const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const C={};function inc(k,n){C[k]=(C[k]||0)+(n||1);}
let files=[];
for(const d of ['','blog','checkout']){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;
for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
function stripDivBlocksRe(s,re){
let out=s,m;
while((m=re.exec(out))){
  let i=m.index,depth=0,j=i,end=-1;
  while(j<out.length){
    if(out.startsWith('<div',j)){depth++;j=out.indexOf('>',j)+1;}
    else if(out.startsWith('</div>',j)){depth--;j+=6;if(depth===0){end=j;break;}}
    else j++;
    if(j<=0)break;}
  if(end>0){out=out.slice(0,i)+out.slice(end);inc('testimonialBlocks');re.lastIndex=0;}else break;}
return out;}
for(const f of files){
let s=fs.readFileSync(f,'utf8');const o=s;
if(/testimonial/.test(s)&&/Sarah M\.|Jake M\.|Marcus R\.|Priya M\.|Dev L\.|Nicole P\.|Jamie T\.|Sarah C\.|Sarah K\.|Mike T\.|Jessica R\./.test(s)){
  s=stripDivBlocksRe(s,/<div class="[^"]*testimonial[^"]*"/g);}
s=s.replace(/,?\s*"aggregateRating":\s*\{[^{}]*\}/g,m=>{inc('aggRegex');return '';});
if(s.includes('�')){s=s.replace(/ ?�\??"? ?/g,' — ');inc('moji');}
if(s!==o)fs.writeFileSync(f,s,'utf8');}
console.log(JSON.stringify(C));
let fake=0,agg=0;
for(const f of files){const s=fs.readFileSync(f,'utf8');
if(/Sarah M\.|Jake M\.|Marcus R\.|Priya M\.|Nicole P\.|Jamie T\./.test(s))fake++;
agg+=s.split('aggregateRating').length-1;}
console.log('LEFT fakeNameFiles:',fake,'aggregateRating:',agg);
