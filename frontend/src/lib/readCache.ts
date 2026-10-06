/** Session-memory only. Never persist credentials, mutations, or health verdicts. */
export class ReadCache {
  private entries = new Map<string, {data: unknown; at: number; bytes: number}>();
  private pending = new Map<string, Promise<unknown>>();
  private errors = new Map<string, string>();
  private bytes = 0;
  constructor(private limit = 48, private byteLimit = 64 * 1024 * 1024) {}
  error(key: string) { return this.errors.get(key) ?? ''; }
  peek<T>(key: string) {
    const item = this.entries.get(key);
    if (item) { this.entries.delete(key); this.entries.set(key, item); }
    return item as {data: T; at: number} | undefined;
  }
  read<T>(key: string, loader: () => Promise<T>, maxAge = 0): Promise<T> {
    const item = this.peek<T>(key);
    if (item && !this.errors.has(key) && Date.now() - item.at < maxAge) return Promise.resolve(item.data);
    const pending = this.pending.get(key);
    if (pending) return pending as Promise<T>;
    const request = loader().then(data => {
      this.errors.delete(key);
      const bytes = JSON.stringify(data).length * 2;
      this.bytes -= this.entries.get(key)?.bytes ?? 0;
      this.entries.delete(key);
      if (bytes <= this.byteLimit) {
        this.entries.set(key, {data, at: Date.now(), bytes}); this.bytes += bytes;
      }
      while (this.entries.size > this.limit || this.bytes > this.byteLimit) {
        const oldest = this.entries.keys().next().value!;
        this.bytes -= this.entries.get(oldest)!.bytes; this.entries.delete(oldest);
      }
      return data;
    }).catch(error => {
      this.errors.delete(key); this.errors.set(key, String(error.message ?? error));
      while(this.errors.size > this.limit)this.errors.delete(this.errors.keys().next().value!);
      throw error;
    }).finally(() => { this.pending.delete(key); });
    this.pending.set(key, request);
    return request;
  }
}
export const readCache = new ReadCache();
