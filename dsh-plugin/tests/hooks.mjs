/**
 * Module hooks：把 @deepseek-ai/dsh-llm 换成 stub（单测用）。
 */
const STUB = `
export function createUserMessage(input) {
  return {
    content: input.content,
    source: input.source ?? { kind: 'user' },
    role: 'user',
    id: 'stub-' + Math.random().toString(36).slice(2),
  };
}
`;
const URL_STUB = 'data:text/javascript;base64,' + Buffer.from(STUB).toString('base64');

export async function resolve(specifier, context, next) {
  if (specifier === '@deepseek-ai/dsh-llm') {
    return { url: URL_STUB, shortCircuit: true };
  }
  return next(specifier, context);
}
