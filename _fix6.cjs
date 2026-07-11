const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const SOLO='https://buy.stripe.com/9B64gzfbfenL8U7dq66kg28?prefilled_promo_code=FLASH40';
const FPRO='fZueVd3sxgvTb2f2Ls6kg0v';
const C={};function inc(k,n){C[k]=(C[k]||0)+(n||1);}
let files=[];
for(const d of ['','blog','checkout']){const p=path.join(ROOT,d);if(!fs.existsSync(p))continue;
for(const f of fs.readdirSync(p)){if(f.endsWith('.html'))files.push(path.join(p,f));}}
// helper: remove balanced div blocks starting with a marker
function stripDivBlocks(s,marker){
let out=s,i;
while((i=out.indexOf(marker))>=0){
  let depth=0,j=i,end=-1;
  while(j<out.length){
    if(out.startsWith('<div',j)){depth++;j=out.indexOf('>',j)+1;}
    else if(out.startsWith('</div>',j)){depth--;j+=6;if(depth===0){end=j;break;}}
    else j++;
    if(j<=0)break;
  }
  if(end>0){out=out.slice(0,i)+out.slice(end);inc('testiCards');}else break;
}
return out;}
for(const f of files){
let s=fs.readFileSync(f,'utf8');const o=s;
// 1. dead / stale stripe links -> canonical active ones
for(const dead of ['eVa9Ek5OOaGF1EkcMM','3cs5nS3pl9Xp7RSdQQ','eVqaEX6EJenLfiv5XE6kg0u','fZu4gz3sxbbzeer3Pw6kg0t','3cI14n3sxa7v4DRgCi6kg0s']){
  const n=s.split(dead).length-1;if(n){inc('deadLinks',n);s=s.split(dead).join(FPRO);}}
// 2. solofounder placeholders + gumroad
s=s.replace(/https?:\/\/buy\.stripe\.com\/SOLOFOUNDEROS[A-Z_]*/g,m=>{inc('soloPlaceholder');return SOLO;});
s=s.replace(/https?:\/\/(www\.)?(markmma1985\.)?gumroad\.com\/l\/solofounderos/g,m=>{inc('soloGumroad');return SOLO;});
// 3. solofounder $9 -> $39 (homepage card)
s=s.replace(/Get SoloFounderOS (&mdash;|—) \$9\b/g,m=>{inc('soloPrice');return m.replace('$9','$39');});
s=s.replace(/\$9 (one-time )?(&middot;|·) lifetime access/g,m=>{inc('soloPrice');return m.replace('$9','$39');});
s=s.replace(/background-clip:text;">\$9<\/span>/g,m=>{inc('soloPrice');return m.replace('$9','$39');});
if(path.basename(f)==='solofounderos.html'&&f.includes('checkout')){
  s=s.replace(/\$9\b(?!\d)/g,m=>{inc('soloCheckout9');return '$39';});}
// 4. JSON-LD: parse & strip fake rating/review
s=s.replace(/(<script type="application\/ld\+json">)([\s\S]*?)(<\/script>)/g,(m,a,body,z)=>{
  try{const j=JSON.parse(body);
    const scrub=(node)=>{if(Array.isArray(node)){node.forEach(scrub);return;}
      if(node&&typeof node==='object'){
        if(node.aggregateRating){delete node.aggregateRating;inc('aggRating');}
        if(node.review){delete node.review;inc('reviewSchema');}
        if(node['@graph'])scrub(node['@graph']);}};
    scrub(j);
    return a+'\n  '+JSON.stringify(j,null,2)+'\n  '+z;
  }catch(e){inc('ldParseFail');return m;}
});
// 5. remove fake testimonial cards; honest note in empty grids
s=stripDivBlocks(s,'<div class="testi-card"');
s=s.replace(/<div class="testi-grid">\s*<\/div>/g,'<div class="testi-grid"><div style="grid-column:1/-1;text-align:center;padding:1.5rem;color:var(--text-secondary,#94a3b8);border:1px dashed rgba(124,58,237,.35);border-radius:12px;">This template is new — no reviews yet. Every purchase is covered by a 30-day, no-questions money-back guarantee.</div></div>');
// 6. de-link removed fake pages
s=s.replace(/<a\b[^>]*href="[^"]*(?:testimonials|wall-of-love|case-study|index-backup)(?:\.html)?"[^>]*>[\s\S]{0,300}?<\/a>/g,m=>{inc('deadAnchors');return '';});
// 7. thank-you honesty
if(path.basename(f)==='thank-you.html'){
  s=s.replace(/arrives within 5 minutes/g,'usually arrives within a few hours');
  s=s.replace(/is being sent to your inbox right now/g,'is on its way to your inbox');
  inc('thankyou');}
// 8. analytics snippet
if(!s.includes('/_vercel/insights')&&s.includes('</body>')){
  s=s.replace('</body>','<script defer src="/_vercel/insights/script.js"></script>\n</body>');inc('analytics');}
if(s!==o)fs.writeFileSync(f,s,'utf8');
}
// 9. delete fake pages + backup junk
for(const p of ['testimonials.html','wall-of-love.html','case-study.html','index-backup.html']){
  const fp=path.join(ROOT,p);if(fs.existsSync(fp)){fs.unlinkSync(fp);inc('deletedPages');}}
// 10. vercel.json redirects for deleted pages
const vj=JSON.parse(fs.readFileSync(path.join(ROOT,'vercel.json'),'utf8'));
vj.redirects=vj.redirects||[];
for(const src of ['/testimonials','/wall-of-love','/case-study','/index-backup']){
  if(!vj.redirects.find(r=>r.source===src)){vj.redirects.push({source:src,destination:'/trust-page',permanent:true});
  vj.redirects.push({source:src+'.html',destination:'/trust-page',permanent:true});}}
fs.writeFileSync(path.join(ROOT,'vercel.json'),JSON.stringify(vj,null,1),'utf8');
console.log(JSON.stringify(C,null,1));
// leftover audit
let left={eVa9Ek:0,cs5nS:0,SOLOFOUNDEROS:0,aggregateRating:0,'testi-card':0,fakeNames:0};
for(const f of files.filter(f=>fs.existsSync(f))){const s=fs.readFileSync(f,'utf8');
for(const k of Object.keys(left)){if(k==='fakeNames'){if(/Sarah M\.|Jake M\.|Marcus R\.|Priya M\./.test(s))left[k]++;}
else left[k]+=s.split(k==='eVa9Ek'?'eVa9Ek':k==='cs5nS'?'3cs5nS':k).length-1;}}
console.log('LEFT',JSON.stringify(left));
