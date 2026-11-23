import test from 'node:test';
import assert from 'node:assert/strict';
import {extractSourceCitations,groupSourceRanges,readableMarkdownCopy,sourceExplorerUrl} from '../src/utils/sourceCitations.js';

const sources=[
  {citation_id:'S1',file_path:'src/app.py',start_line:10,end_line:18},
  {citation_id:'S2',file_path:'src/app.py',start_line:17,end_line:24},
  {citation_id:'S3',file_path:'README.md',start_line:4,end_line:8},
];

test('moves citation markers out of prose and deduplicates overlapping ranges by file',()=>{
  const result=extractSourceCitations('The endpoint validates input [S1]. It returns JSON [S2, S3].',sources);
  assert.equal(result.markdown,'The endpoint validates input. It returns JSON.');
  assert.deepEqual(result.references,[
    {file_path:'README.md',ranges:[{start:4,end:8}]},
    {file_path:'src/app.py',ranges:[{start:10,end:24}]},
  ]);
});

test('preserves ordinary Markdown links and citation-like text in code',()=>{
  const markdown='See [S1](https://example.test) and `[S2]` for details.\n\n```md\n[S3]\n```';
  const result=extractSourceCitations(markdown,sources);
  assert.equal(result.markdown,markdown);
  assert.deepEqual(result.references.map(item=>item.file_path),['README.md','src/app.py']);
});

test('normalizes legacy filename citations only when safely recognized',()=>{
  const result=extractSourceCitations('Use the handler [app.py · lines 11-15].\n\n[Ordinary](https://example.test).',sources);
  assert.equal(result.markdown,'Use the handler.\n\n[Ordinary](https://example.test).');
  assert.equal(result.references.find(item=>item.file_path==='src/app.py').ranges[0].start,10);
});

test('groups exact duplicates and overlapping intervals while retaining separate ranges',()=>{
  assert.deepEqual(groupSourceRanges([
    {file_path:'x.py',start_line:2,end_line:5},{file_path:'x.py',start_line:2,end_line:5},
    {file_path:'x.py',start_line:4,end_line:8},{file_path:'x.py',start_line:12,end_line:13},
  ]),[{file_path:'x.py',ranges:[{start:2,end:8},{start:12,end:13}]}]);
});

test('plain Markdown copy contains a single final references section without inline markers',()=>{
  const copy=readableMarkdownCopy('# Summary\n\nClaim [S1] and this [guide](https://example.test).',sources);
  assert.match(copy,/Claim  and this \[guide\]\(https:\/\/example\.test\)\./);
  assert.equal((copy.match(/Referenced Sources/g)||[]).length,1);
  assert.match(copy,/- src\/app\.py — Lines 10–24/);
});

test('source references navigate to the correct repository file and first cited line',()=>{
  assert.equal(sourceExplorerUrl(7,'src/my file.py',23),'/repos/7/explorer?file=src%2Fmy%20file.py&line=23');
});
