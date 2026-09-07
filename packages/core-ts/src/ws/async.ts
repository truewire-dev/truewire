/** Small async primitives the socket classes are built from. */

/** A promise with its `resolve`/`reject` exposed; settling twice is a no-op. */
export class Deferred<T> {
  readonly promise: Promise<T>
  #resolve!: (value: T | PromiseLike<T>) => void
  #reject!: (reason: unknown) => void
  #settled = false

  constructor() {
    this.promise = new Promise<T>((resolve, reject) => { this.#resolve = resolve; this.#reject = reject })
  }

  get settled(): boolean { return this.#settled }

  resolve(value: T | PromiseLike<T>): void {
    if (!this.#settled) { this.#settled = true; this.#resolve(value) }
  }

  reject(reason: unknown): void {
    if (!this.#settled) { this.#settled = true; this.#reject(reason) }
  }
}

/** An unbounded FIFO whose `pull()` waits for the next item. */
export class AsyncQueue<T> {
  readonly #items: T[] = []
  readonly #waiters: Deferred<T>[] = []

  get size(): number { return this.#items.length }

  push(item: T): void {
    const waiter = this.#waiters.shift()
    if (waiter) waiter.resolve(item)
    else this.#items.push(item)
  }

  pull(): Promise<T> {
    if (this.#items.length) return Promise.resolve(this.#items.shift()!)
    const waiter = new Deferred<T>()
    this.#waiters.push(waiter)
    return waiter.promise
  }
}

/** A mutex: `run` executes `fn` once every earlier `run` has finished. */
export class Lock {
  #tail: Promise<unknown> = Promise.resolve()

  get locked(): boolean { return this.#locked }
  #locked = false

  run<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.#tail.then(async () => {
      this.#locked = true
      try { return await fn() } finally { this.#locked = false }
    })
    this.#tail = result.catch(() => {})
    return result
  }
}
