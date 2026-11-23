const DOCUMENT_LABELS = {
  overview: 'Project_Overview',
  readme: 'README_Documentation',
  api: 'API_Documentation',
  architecture: 'Architecture_Documentation',
  module: 'Module_Documentation',
};

export function markdownFilename(repositoryName, kind) {
  const repository = String(repositoryName || 'Repository')
    .trim()
    .replace(/[^a-z0-9]+/gi, '_')
    .replace(/^_+|_+$/g, '') || 'Repository';
  const label = DOCUMENT_LABELS[kind] || String(kind || 'Documentation')
    .replace(/[^a-z0-9]+/gi, '_')
    .replace(/^_+|_+$/g, '') || 'Documentation';
  return `${repository}_${label}.md`;
}

/** Download the complete Markdown string without transforming its contents. */
export function downloadMarkdown(markdown, filename, {
  documentRef = globalThis.document,
  urlApi = globalThis.URL,
  schedule = globalThis.setTimeout,
} = {}) {
  if (typeof markdown !== 'string' || !markdown.trim()) return false;
  if (!documentRef?.createElement || !urlApi?.createObjectURL || typeof Blob === 'undefined') return false;

  let objectUrl;
  let link;
  try {
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
    objectUrl = urlApi.createObjectURL(blob);
    link = documentRef.createElement('a');
    link.href = objectUrl;
    link.download = filename;
    link.style?.setProperty?.('display', 'none');
    documentRef.body?.appendChild?.(link);
    link.click();
    return true;
  } catch {
    return false;
  } finally {
    link?.remove?.();
    if (objectUrl) schedule(() => urlApi.revokeObjectURL(objectUrl), 1000);
  }
}

export function exportGeneratedDocument(generatedDocument, repositoryName, browserApis) {
  if (!generatedDocument || typeof generatedDocument.content !== 'string') return false;
  return downloadMarkdown(
    generatedDocument.content,
    markdownFilename(repositoryName, generatedDocument.kind),
    browserApis,
  );
}
