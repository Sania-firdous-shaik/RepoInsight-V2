import assert from 'node:assert/strict';
import test from 'node:test';
import {validateArchitectureGraph} from '../src/utils/architectureValidation.js';

function graph(){
  const source={file_path:'app/main.py',start_line:1,end_line:4};
  return {version:1,repository:{id:1,name:'owner/repo'},sources:[source],metadata:{},nodes:[
    {id:'layer:api',label:'API',type:'layer',parent_id:null,citations:[source]},
    {id:'database:store',label:'Database',type:'database',parent_id:null,citations:[source]},
  ],edges:[{id:'edge:db',source:'layer:api',target:'database:store',type:'connects_to',verified:true,citations:[source]}]};
}

test('accepts only graph components and directed relationships with indexed source citations',()=>{
  const value=graph();assert.equal(validateArchitectureGraph(value),value);
  const bad=graph();bad.edges[0].citations=[{file_path:'missing.py',start_line:2,end_line:3}];
  assert.throws(()=>validateArchitectureGraph(bad),/edge citation is missing/);
});

test('rejects unknown nodes, endpoints, unsupported relationships and duplicate ids',()=>{
  const badNode=graph();badNode.nodes[0].type='unknown';assert.throws(()=>validateArchitectureGraph(badNode),/unsupported node type/);
  const badEdge=graph();badEdge.edges[0].target='missing';assert.throws(()=>validateArchitectureGraph(badEdge),/invalid edge endpoint/);
  const badType=graph();badType.edges[0].type='unverified';assert.throws(()=>validateArchitectureGraph(badType),/unsupported or unverified relationship/);
  const duplicate=graph();duplicate.nodes[1].id=duplicate.nodes[0].id;assert.throws(()=>validateArchitectureGraph(duplicate),/duplicate node id/);
});
