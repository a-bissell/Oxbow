// Exercise the actual candidate detail React tree, not just URL helpers.
import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
const temp = mkdtempSync(join(tmpdir(), 'oxbow-render-'));
try {
  const result = execFileSync(process.env.PYTHON || 'python', ['-c', `
from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import WorkRef, Provenance
cfg = load_config('default', use_env=False, overrides={'llm': {'provider': 'none'}})
c = Cache(':memory:'); load_fixtures(cfg, c)
r = run_triage('Find oxide dielectrics', cfg, cache=c, offline=True)
s = r.shortlist[0]
s.record.literature.sample_works = [WorkRef(work_id=str(i), title=title, doi=doi) for i, (title, doi) in enumerate([
 ('Bare DOI', '10.1063/1.3634052'), ('Full DOI', 'https://doi.org/10.1063/1.3634052'),
 ('Missing DOI', None), ('Malformed DOI', 'https://doi.org/https://doi.org/10.1063/bad')])]
s.record.band_gap.provenance = Provenance(source='materials_project', url='https://materialsproject.org/materials/mp-1')
print(r.model_dump_json())
`], { cwd: resolve('..'), env: {...process.env, OXIDE_TRIAGE_SITE_CONFIG: 'off'}, encoding: 'utf8' });
  writeFileSync(join(temp, 'result.json'), result);
  await build({
    stdin: { contents: String.raw`
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync, writeFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import Canvas from './src/components/Canvas';
const result = JSON.parse(readFileSync(process.argv[2], 'utf8'));
globalThis.__testApp = { currentResultId: 'test', results: {test: result}, prevOf: {}, view: {kind: 'focus', candidate: result.shortlist[0].record.material_id}, busy: false };
const html = renderToStaticMarkup(<Canvas />);
assert.equal((html.match(/href="https:\/\/doi.org\/10.1063\/1.3634052"/g) || []).length, 2);
assert.ok(!html.includes('doi.org/https'));
assert.ok(html.includes('Malformed DOI') && html.includes('Missing DOI'));
assert.ok(html.includes('support for a particular property has not been established'));
assert.ok(html.includes('high data coverage') && !html.includes('high confidence'));
assert.ok(html.includes('https://materialsproject.org/materials/mp-1'));
assert.ok(html.includes('corrected'));
assert.ok(html.includes('bulk hull vs Si:') && html.includes('0.05 eV/atom tolerance'));
assert.ok(html.includes('bulk thermodynamics only'));
assert.ok(!html.includes('fine on Si'));
if (process.argv[3]) writeFileSync(process.argv[3], '<!doctype html><html><head><meta charset="utf-8"><style>' + readFileSync('./src/styles.css', 'utf8') + '</style></head><body>' + html + '</body></html>');
console.log('Rendered candidate detail: DOI links, source distinction, coverage and correction labels passed.');
`, resolveDir: process.cwd(), loader: 'tsx' },
    bundle: true, platform: 'node', format: 'cjs', outfile: join(temp, 'render.cjs'),
    plugins: [{name: 'test-store', setup(b) { b.onLoad({filter: /\/store\.tsx$/}, () => ({contents: 'export function useApp() { return globalThis.__testApp; }', loader: 'js'})); }}],
  });
  console.log(execFileSync(process.execPath, [join(temp, 'render.cjs'), join(temp, 'result.json'), ...(process.env.RENDER_OUT ? [process.env.RENDER_OUT] : [])], {encoding: 'utf8'}));
} finally { rmSync(temp, {recursive:true, force:true}); }
