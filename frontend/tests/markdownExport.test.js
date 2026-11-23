import assert from 'node:assert/strict';
import test from 'node:test';
import {exportGeneratedDocument,markdownFilename} from '../src/utils/markdownExport.js';

function browserHarness() {
  const calls={created:[],clicked:[],revoked:[],appended:[],removed:[]};
  const doc={
    body:{appendChild(link){calls.appended.push(link)}},
    createElement(tag){
      assert.equal(tag,'a');
      return {style:{setProperty(){}},click(){calls.clicked.push({href:this.href,download:this.download})},remove(){calls.removed.push(this)}};
    },
  };
  const urlApi={
    createObjectURL(blob){const url=`blob:download-${calls.created.length}`;calls.created.push({blob,url});return url},
    revokeObjectURL(url){calls.revoked.push(url)},
  };
  return {calls,documentRef:doc,urlApi,schedule(callback,delay){assert.equal(delay,1000);callback()}};
}

const markdown='# Hastakala\n\n## API\n\n| Route | Method |\n| --- | --- |\n| `/api/auth` | POST |\n\n```js\nconst token = "[S1]";\n```\n\nSource: craft-app-backend/routes/auth.js [S1]';

test('all five generated document types download complete original Markdown',async()=>{
  const kinds=['overview','readme','api','architecture','module'];
  const labels=['Project_Overview','README_Documentation','API_Documentation','Architecture_Documentation','Module_Documentation'];
  for(let i=0;i<kinds.length;i++){
    const browser=browserHarness();
    const generatedResponse={kind:kinds[i],content:markdown,sources:[{file_path:'source.js'}]};
    assert.equal(exportGeneratedDocument(generatedResponse,'Hastakala',browser),true);
    assert.equal(await browser.calls.created[0].blob.text(),markdown);
    assert.deepEqual(browser.calls.clicked,[{href:browser.calls.created[0].url,download:`Hastakala_${labels[i]}.md`}]);
    assert.equal(browser.calls.appended.length,1);
    assert.equal(browser.calls.removed.length,1);
    assert.deepEqual(browser.calls.revoked,[browser.calls.created[0].url]);
  }
});

test('filename uses a safe repository label and meaningful generated type',()=>{
  assert.equal(markdownFilename('Hastakala / App','overview'),'Hastakala_App_Project_Overview.md');
});

test('missing or empty content does not create or trigger a download',()=>{
  const browser=browserHarness();
  assert.equal(exportGeneratedDocument({kind:'overview',content:''},'Hastakala',browser),false);
  assert.equal(exportGeneratedDocument({kind:'overview'},'Hastakala',browser),false);
  assert.equal(browser.calls.created.length,0);
  assert.equal(browser.calls.clicked.length,0);
});
