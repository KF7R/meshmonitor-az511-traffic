const fs = require('fs');
const path = require('path');
let ts;
try { ts = require('typescript'); } catch { ts = require('./test-runtime/node_modules/typescript'); }
const base = path.resolve(process.argv[2] ?? path.join(__dirname, 'meshmonitor'));
const dest = path.resolve(__dirname, 'native-modules');
const seen = new Set();
function compile(rel) {
  if (seen.has(rel)) return;
  seen.add(rel);
  let source = path.join(base, rel);
  if (!fs.existsSync(source)) throw Error('Missing '+source);
  const code = ts.transpileModule(fs.readFileSync(source,'utf8'), {
    compilerOptions: {module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022}
  }).outputText;
  const output = path.join(dest, rel.replace(/\.ts$/,'.js'));
  fs.mkdirSync(path.dirname(output), {recursive:true});
  fs.writeFileSync(output, code);
  for (const match of code.matchAll(/(?:from\s*|import\s*)['"]([^'"]+)['"]/g)) {
    const imp = match[1];
    if (imp.startsWith('.')) {
      const dep = path.normalize(path.join(path.dirname(rel),imp));
      compile(dep.replace(/\.js$/,'.ts'));
    } else if (imp !== 're2' && !imp.startsWith('node:')) {
      throw Error('Unexpected external dependency: '+imp);
    }
  }
}
for (const rel of ['src/types/automation.ts',
  'src/server/services/automation/graphEvaluator.ts',
  'src/server/services/automation/conditionEvaluator.ts',
  'src/server/services/automation/actionExecutor.ts']) compile(rel);
fs.writeFileSync(path.join(dest,'package.json'),JSON.stringify({type:'module'}));
// None of these tests exercises regex conditions; any such use fails loudly.
const re2 = path.join(dest,'node_modules/re2');
fs.mkdirSync(re2,{recursive:true});
fs.writeFileSync(path.join(re2,'package.json'),JSON.stringify({type:'module',main:'index.js'}));
fs.writeFileSync(path.join(re2,'index.js'),`export default class RE2 { constructor() { throw Error('regex is outside this isolated harness'); } }`);
console.log('Transpiled '+seen.size+' release source modules; IO dependencies supplied by test harness.');
