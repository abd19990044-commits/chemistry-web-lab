import { describe, expect, it } from "vitest";
import { PayloadTooLargeError, readLimitedJsonBody } from "../src/body";

function chunkedRequest(chunks: Uint8Array[], onCancel = () => {}): Request {
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index === chunks.length) controller.close();
      else controller.enqueue(chunks[index++]);
    },
    cancel: onCancel,
  });
  // Node's Request implementation requires duplex for streaming request bodies.
  return new Request("https://lab.example/api/jobs", { method: "POST", body, duplex: "half" } as RequestInit);
}

const encode = (text: string) => new TextEncoder().encode(text);

describe("bounded JSON request reader", () => {
  it("accepts exact byte limit and decodes multibyte characters across chunks", async () => {
    const bytes = encode('{"name":"é🧪"}');
    const request = chunkedRequest([bytes.slice(0, 10), bytes.slice(10, 13), bytes.slice(13)]);
    await expect(readLimitedJsonBody(request, bytes.length)).resolves.toEqual({ name: "é🧪" });
  });

  it("rejects unicode that fits a character cap but exceeds the byte cap", async () => {
    const text = '{"name":"' + "🧪".repeat(30) + '"}';
    expect(text.length).toBeLessThan(100);
    await expect(readLimitedJsonBody(chunkedRequest([encode(text)]), 100)).rejects.toBeInstanceOf(PayloadTooLargeError);
  });

  it("cancels an oversized stream without consuming all chunks", async () => {
    let cancelled = false;
    const chunks = Array.from({ length: 100 }, () => encode("12345678"));
    const request = chunkedRequest(chunks, () => { cancelled = true; });
    await expect(readLimitedJsonBody(request, 16)).rejects.toBeInstanceOf(PayloadTooLargeError);
    expect(cancelled).toBe(true);
    expect(request.body?.locked).toBe(false);
  });

  it("rejects a forged small Content-Length based on actual streamed bytes", async () => {
    const request = chunkedRequest([encode('{"data":"too much data"}')]);
    request.headers.set("Content-Length", "1");
    await expect(readLimitedJsonBody(request, 8)).rejects.toBeInstanceOf(PayloadTooLargeError);
  });

  it("rejects a declared oversized body without reading", async () => {
    const request = new Request("https://lab.example/api/jobs", { method: "POST", body: "{}", headers: { "Content-Length": "999" } });
    await expect(readLimitedJsonBody(request, 16)).rejects.toBeInstanceOf(PayloadTooLargeError);
    expect(request.bodyUsed).toBe(false);
  });

  it("handles empty bodies and rejects malformed JSON or UTF-8", async () => {
    await expect(readLimitedJsonBody(new Request("https://lab.example"), 16)).resolves.toBeNull();
    await expect(readLimitedJsonBody(chunkedRequest([encode("  ")]), 16)).resolves.toBeNull();
    await expect(readLimitedJsonBody(chunkedRequest([encode("{broken")]), 16)).rejects.toThrow();
    await expect(readLimitedJsonBody(chunkedRequest([new Uint8Array([0xff])]), 16)).rejects.toThrow();
  });
});
