// Count bytes while streaming, before decoding or accumulating an entire body.
// Content-Length is optional and cannot be trusted as the only size check.
export class PayloadTooLargeError extends Error {
  constructor() {
    super("Payload exceeds maximum size limit of 1 MB.");
    this.name = "PayloadTooLargeError";
  }
}

export async function readLimitedJsonBody(request: Request, maxBytes: number): Promise<unknown> {
  const declaredLength = request.headers.get("content-length");
  if (declaredLength && Number(declaredLength) > maxBytes) {
    throw new PayloadTooLargeError();
  }
  if (!request.body) return null;

  const reader = request.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });
  const parts: string[] = [];
  let bytesRead = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytesRead += value.byteLength;
      if (bytesRead > maxBytes) {
        await reader.cancel().catch(() => {});
        throw new PayloadTooLargeError();
      }
      parts.push(decoder.decode(value, { stream: true }));
    }
    parts.push(decoder.decode());
  } finally {
    reader.releaseLock();
  }
  const text = parts.join("");
  return text.trim() ? JSON.parse(text) : null;
}
