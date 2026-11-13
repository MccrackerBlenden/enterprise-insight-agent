const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'../src');
const files=['styles.css','operations.css','operations/p2c3-responsive.css'];
let count=0;
for (const name of files){
  const content=fs.readFileSync(path.join(root,name),'utf8');
  for (const m of content.matchAll(/font-size\s*:\s*(\d+(?:\.\d+)?)px/g)){
    count++;
    if(Number(m[1]) < 12) throw new Error(`${name}: font-size ${m[1]}px is below the 12px floor`);
  }
}
for (const f of ['eia-brand-icon.png','favicon.png']){
  if (!fs.existsSync(path.resolve(__dirname, '../public',f))) throw new Error('Missing brand asset: '+f);
}
const app=fs.readFileSync(path.join(root,'App.tsx'),'utf8');
if(!app.includes('/eia-brand-icon.png')) throw new Error('Real brand icon not used in App');
console.log(`VISUAL CONTRACT PASS: ${count} explicit font sizes >= 12px; brand assets present`);
