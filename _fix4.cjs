const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
let files=[];for(const d of ['','blog','checkout']){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
let n=0;for(const f of files){let s=fs.readFileSync(f,'utf8');const o=s;
s=s.replace(/1,200\+ Customers/g,'Lifetime access');
s=s.replace(/[\d,]{3,7}\+ (freelancers|founders|builders|creators|customers|users|members)/gi,'$1');
if(s!==o){fs.writeFileSync(f,s,'utf8');n++;}}
let left=0;for(const f of files){left+=(fs.readFileSync(f,'utf8').match(/[\d,]{3,7}\+ (freelancers|founders|builders|creators|customers|users|members)/gi)||[]).length;}
console.log('pass4 changed:',n,'leftover:',left);
