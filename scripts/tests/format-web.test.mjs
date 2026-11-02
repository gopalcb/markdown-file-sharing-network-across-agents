import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import prettier from 'prettier';
import { expandControlBodies, separateSteps } from '../format-web.mjs';

test('formatting preserves nested control flow, comments and template literals', async () => {
  const source = `
    let result = [];
    for (const a of [true, false])
      if (a) if (false) result.push('wrong'); else result.push('yes');
      else result.push('no');
    const literal = \`line one
if (untouched) raw text\`;
    // Keep this comment with the call.
    result.push(literal);
    result.join('|');
  `;
  const format = async (text) =>
    separateSteps(
      await prettier.format(expandControlBodies(text, 'fixture.ts'), { parser: 'typescript' }),
      'fixture.ts',
    );
  const formatted = await format(source);

  assert.equal(vm.runInNewContext(formatted), vm.runInNewContext(source));
  assert.equal(await format(formatted), formatted);
  assert.match(formatted, /\n\n\/\/ Keep this comment/);
  assert.match(formatted, /line one\nif \(untouched\) raw text/);
});
