const renderCache = new Map();
let renderSequence = 0;

function quoteNodeLabel(label) {
  const value = label.trim();
  if (!value || (/^(?:"[\s\S]*"|'[\s\S]*')$/).test(value)) return value;
  return `"${value.replaceAll('\\', '\\\\').replaceAll('"', '&quot;').replaceAll('\n', ' ')}"`;
}

function quoteNodeLabels(line) {
  const nodeStart = /(?<![\w$])([A-Za-z_][\w$.-]*)\s*(\[\(|\[\[|\{\{|\[|\(|\{)/g;
  let output = '';
  let cursor = 0;
  let match;
  while ((match = nodeStart.exec(line))) {
    const id = match[1];
    const opener = match[2];
    const closeFor = { '[(': ')]', '[[': ']]', '{{': '}}', '[': ']', '(': ')', '{': '}' }[opener];
    const labelStart = nodeStart.lastIndex;
    const closeIndex = line.indexOf(closeFor, labelStart);
    if (closeIndex < 0) continue;
    const label = line.slice(labelStart, closeIndex);
    // Keep a quoted label intact. Other labels are quoted so parser-sensitive
    // punctuation, parentheses and filenames are treated as display text.
    output += line.slice(cursor, labelStart) + quoteNodeLabel(label) + closeFor;
    cursor = closeIndex + closeFor.length;
    nodeStart.lastIndex = cursor;
  }
  return output ? output + line.slice(cursor) : line;
}

function separateNodeStatements(line) {
  // Models sometimes place declarations next to each other on one line:
  // `A[Backend Server (index.js)] B_DB[(PostgreSQL)]`. Mermaid requires a
  // statement boundary there. Do not split ordinary edge operators.
  return line.replace(/([\]\)}])\s+(?=[A-Za-z_][\w$.-]*\s*(?:\[\(|\[\[|\{\{|\[|\(|\{|--|==|-\.|~~~|o--|x--|--o|--x))/g, '$1\n  ');
}

export function normalizeMermaidSource(source) {
  let text = String(source ?? '').replace(/^\s*```(?:mermaid)?\s*/i, '').replace(/\s*```\s*$/, '').trim();
  if (!text) return '';
  const lines = text.replaceAll('\r\n', '\n').replaceAll('\r', '\n').split('\n');
  const normalized = [];
  for (let rawLine of lines) {
    let line = quoteNodeLabels(rawLine);
    const header = line.match(/^(\s*(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)\s+)(?=[A-Za-z_][\w$.-]*\s*(?:\[\(|\[\[|\{\{|\[|\(|\{))/i);
    if (header) line = `${header[1].trimEnd()}\n  ${line.slice(header[0].length)}`;
    normalized.push(...line.split('\n').flatMap(part => separateNodeStatements(part).split('\n')));
  }
  return normalized.map(line => line.trimEnd()).join('\n').trim();
}

export function renderMermaidOnce(source, mermaid) {
  const normalized = normalizeMermaidSource(source);
  if (renderCache.has(normalized)) return renderCache.get(normalized);
  const rendering = (async () => {
    try {
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' });
      const parsed = await mermaid.parse(normalized, { suppressErrors: true });
      if (!parsed) return { error: true };
      const result = await mermaid.render(`repo-architecture-${++renderSequence}`, normalized);
      return result?.svg ? { svg: result.svg } : { error: true };
    } catch {
      return { error: true };
    }
  })();
  renderCache.set(normalized, rendering);
  return rendering;
}
