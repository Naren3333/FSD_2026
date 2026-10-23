// generate browser types from the backend contracts; check mode reports stale files.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import openapiTS, { astToString } from 'openapi-typescript';
import prettier from 'prettier';
import ts from 'typescript';

const SERVICES = [
  'identity',
  'curriculum',
  'content',
  'assessment',
  'grading',
  'analytics',
  'question-chatbot-agent',
  'question-generation-agent',
  'student-performance-agent',
];
const contracts = new URL('../../../contracts/http/', import.meta.url);
const generated = new URL('../src/api/generated/', import.meta.url);
const check = process.argv.includes('--check');

const stale = [];
await mkdir(generated, { recursive: true });
for (const service of SERVICES) {
  const contract = JSON.parse(await readFile(new URL(`${service}.openapi.json`, contracts), 'utf8'));
  // only the schemas: the app's clients name their own paths.
  const ast = await openapiTS({ ...contract, paths: {} }, { defaultNonNullable: false, silent: true });
  const header = `// generated from contracts/http/${service}.openapi.json; run npm run contracts to refresh.\n\n`;
  const target = new URL(`${service}.ts`, generated);
  const options = await prettier.resolveConfig(fileURLToPath(target));
  const source = ts.createSourceFile(`${service}.ts`, astToString(ast), ts.ScriptTarget.Latest, true);
  const types = ts.createPrinter({ removeComments: true }).printFile(source);
  const text = await prettier.format(header + types, {
    ...options,
    filepath: fileURLToPath(target),
  });
  if (!check) {
    await writeFile(target, text);
  } else if ((await readFile(target, 'utf8').catch(() => '')) !== text) {
    stale.push(`apps/web/src/api/generated/${service}.ts`);
  }
}
if (stale.length) {
  console.error(
    `Out of date; run npm run contracts in apps/web and review the diff:\n  ${stale.join('\n  ')}`,
  );
  process.exit(1);
}
console.log(`${check ? 'Checked' : 'Generated'} ${SERVICES.length} API type files.`);
