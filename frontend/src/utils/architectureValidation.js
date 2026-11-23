const ALLOWED_NODE_TYPES=new Set(['layer','component','route','middleware','service','model','utility','client','entrypoint','configuration','database','external','technology','module','artifact']);
const ALLOWED_EDGE_TYPES=new Set(['imports','api_call','uses_middleware','uses_service','uses_model','references_model','connects_to','calls_external','implemented_with','renders_in']);

export function validateArchitectureGraph(graph){
  if(!graph||graph.version!==1||!Array.isArray(graph.nodes)||!Array.isArray(graph.edges)||!Array.isArray(graph.sources))throw new Error('The architecture service returned an invalid graph.');
  const nodes=new Map(),sources=new Set(graph.sources.map(source=>`${source.file_path}:${source.start_line}-${source.end_line}`));
  for(const node of graph.nodes){
    if(!node||typeof node.id!=='string'||!node.id.trim()||nodes.has(node.id))throw new Error('The architecture graph contains a missing or duplicate node id.');
    if(!ALLOWED_NODE_TYPES.has(node.type))throw new Error('The architecture graph contains an unsupported node type.');
    for(const citation of node.citations||[]){if(!sources.has(`${citation.file_path}:${citation.start_line}-${citation.end_line}`))throw new Error('A node citation is missing from the graph evidence.');}
    nodes.set(node.id,node);
  }
  for(const node of nodes.values())if(node.parent_id&&!nodes.has(node.parent_id))throw new Error('The architecture graph references a missing parent node.');
  const edgeIds=new Set();
  for(const edge of graph.edges){
    if(!edge?.id||edgeIds.has(edge.id))throw new Error('The architecture graph contains a missing or duplicate edge id.');
    if(!nodes.has(edge.source)||!nodes.has(edge.target))throw new Error('The architecture graph contains an invalid edge endpoint.');
    if(!ALLOWED_EDGE_TYPES.has(edge.type)||edge.verified!==true||!Array.isArray(edge.citations)||!edge.citations.length)throw new Error('The architecture graph contains an unsupported or unverified relationship.');
    for(const citation of edge.citations)if(!sources.has(`${citation.file_path}:${citation.start_line}-${citation.end_line}`))throw new Error('An edge citation is missing from the graph evidence.');
    edgeIds.add(edge.id);
  }
  return graph;
}
