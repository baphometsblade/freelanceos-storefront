const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const dirs=['','blog','checkout'];let files=[];
for(const d of dirs){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;
for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
let n=0,left={};
for(const f of files){let s=fs.readFileSync(f,'utf8');const o=s;
s=s.replace(/>2,400\+</g,'>30-day<').replace(/>Founders using it</g,'>Money-back guarantee<');
s=s.replace(/2,400\+ founders/gi,'solo founders').replace(/3,400\+ freelancers/gi,'working freelancers');
s=s.replace(/([\d,]{3,7})\+ (founders|freelancers|builders|creators|customers|users|members) (are using|use|joined|trust)/gi,'$2 $3');
if(s!==o){fs.writeFileSync(f,s,'utf8');n++;}
const m=s.match(/[\d,]{3,7}\+ (founders|freelancers|builders|creators|customers|users|members)/gi);
if(m)left[path.basename(f)]=m.slice(0,3);}
console.log('pass3 changed:',n);
console.log('leftover count-claims:',JSON.stringify(left).slice(0,900));
