const fs=require('fs'),path=require('path');
const ROOT='C:/Users/markm/Desktop/freelanceos-storefront';
const V='https://freelanceos-storefront.vercel.app';
function d(f){return fs.statSync(f).mtime.toISOString().slice(0,10);}
const skip=/^(404|_.*|[A-Z0-9][A-Z0-9-]*[A-Z0-9])\.html$/; // 404, _*, ALL-CAPS internal docs
let urls=[];
for(const f of fs.readdirSync(ROOT)){
  if(!f.endsWith('.html')||skip.test(f))continue;
  const slug=f.replace(/\.html$/,'');
  urls.push([slug==='index'?V+'/':V+'/'+slug, d(path.join(ROOT,f)), slug==='index'?'1.0':'0.8']);
}
const bp=path.join(ROOT,'blog');
if(fs.existsSync(bp))for(const f of fs.readdirSync(bp)){
  if(!f.endsWith('.html'))continue;
  const slug=f.replace(/\.html$/,'');
  urls.push([slug==='index'?V+'/blog/':V+'/blog/'+slug, d(path.join(bp,f)),'0.7']);
}
urls.sort((a,b)=>a[0].localeCompare(b[0]));
let xml='<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n';
for(const[u,lm,pr]of urls)xml+=`  <url><loc>${u}</loc><lastmod>${lm}</lastmod><priority>${pr}</priority></url>\n`;
xml+='</urlset>\n';
fs.writeFileSync(path.join(ROOT,'sitemap.xml'),xml,'utf8');
fs.writeFileSync(path.join(ROOT,'robots.txt'),'User-agent: *\nAllow: /\nDisallow: /checkout/\n\nSitemap: '+V+'/sitemap.xml\n','utf8');
console.log('sitemap URLs:',urls.length,'| robots.txt written');
