/** Split "a, b\nc" (chat channel or server IDs) into a clean list without blanks or repeats. */
export function splitIds(value: string): string[] {
  const out: string[] = [];
  for (const part of value.split(/[,\n]/)) {
    const v = part.trim();
    if (v && !out.includes(v)) out.push(v);
  }
  return out;
}
