const fs=require('fs');const f='index.html';let s=fs.readFileSync(f,'utf8');const o=s;
s=s.replace(/(<div[^>]*>)10,000\+(<\/div>\s*<div[^>]*>)freelancers(<\/div>)/g,(m,a,b,c)=>a+'$0'+b+'to start — free tools'+c);
s=s.replace(/>10,000\+</g,'>$0<');
s=s.replace(/As seen on/g,'Shared on');
s=s.replace(/>Product Hunt</g,'>X (Twitter)<');
s=s.replace(/>\? Try the/g,'>&#9889; Try the');
console.log('changed:',s!==o);fs.writeFileSync(f,s,'utf8');
console.log('10k left:',(s.match(/10,000\+/g)||[]).length,'| PH left:',(s.match(/Product Hunt/g)||[]).length);
