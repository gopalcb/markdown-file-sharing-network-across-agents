/** Format TypeScript without packing control flow or logical call phases together. */
import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import prettier from 'prettier';
import ts from 'typescript';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const check = process.argv.includes('--check');
const config = JSON.parse(readFileSync(join(root, '.prettierrc.json'), 'utf8'));

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);

    return entry.isDirectory() ? sourceFiles(path) : path.endsWith('.ts') ? [path] : [];
  });
}

function applyEdits(source, edits) {
  for (const [position, text] of edits.sort((left, right) => right[0] - left[0])) {
    source = source.slice(0, position) + text + source.slice(position);
  }

  return source;
}

export function expandControlBodies(source, file) {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  const edits = [];

  function wrap(statement) {
    if (statement && !ts.isBlock(statement)) {
      edits.push([statement.getStart(tree), '{\n'], [statement.end, '\n}']);
    }
  }

  function visit(node) {
    if (ts.isIfStatement(node)) {
      wrap(node.thenStatement);

      if (node.elseStatement && !ts.isIfStatement(node.elseStatement)) {
        wrap(node.elseStatement);
      }
    } else if (
      ts.isForStatement(node) ||
      ts.isForOfStatement(node) ||
      ts.isForInStatement(node) ||
      ts.isWhileStatement(node) ||
      ts.isDoStatement(node)
    ) {
      wrap(node.statement);
    }

    ts.forEachChild(node, visit);
  }

  visit(tree);

  return applyEdits(source, edits);
}

export function separateSteps(source, file) {
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  const edits = [];

  function visit(node) {
    const statements = node.statements;

    if (statements && (ts.isBlock(node) || ts.isSourceFile(node))) {
      for (let index = 1; index < statements.length; index++) {
        const statement = statements[index];
        const boundary =
          ts.isIfStatement(statement) ||
          ts.isForStatement(statement) ||
          ts.isForOfStatement(statement) ||
          ts.isForInStatement(statement) ||
          ts.isWhileStatement(statement) ||
          ts.isTryStatement(statement) ||
          ts.isSwitchStatement(statement) ||
          ts.isReturnStatement(statement) ||
          ts.isExpressionStatement(statement);

        if (!boundary) continue;

        const previous = statements[index - 1];
        const gap = source.slice(previous.end, statement.getStart(tree));

        // Insert before comments, keeping their attachment to the next statement.
        if (!/\n\s*\n/.test(gap)) {
          const newline = source.indexOf('\n', previous.end);

          if (newline >= 0 && newline < statement.getStart(tree)) {
            edits.push([newline, '\n']);
          }
        }
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(tree);

  return applyEdits(source, edits);
}

async function main() {
  const files = sourceFiles(join(root, 'telemetry-ui/src'));
  const changed = [];

  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    const expanded = expandControlBodies(source, file);
    const standard = await prettier.format(expanded, { ...config, filepath: file });
    const result = separateSteps(standard, file);

    if (source !== result) {
      changed.push(file);

      if (!check) writeFileSync(file, result);
    }
  }

  if (check && changed.length) {
    throw new Error(`Formatting required: ${changed.join(', ')}`);
  }

  console.log(`TypeScript formatting ${check ? 'checked' : 'applied'}: ${files.length} files`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
