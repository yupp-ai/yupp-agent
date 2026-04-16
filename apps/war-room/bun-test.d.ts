declare module 'bun:test' {
  export function describe(name: string, fn: () => void): void
  export function it(
    name: string,
    fn: () => void | Promise<void>,
    timeout?: number
  ): void
  export function expect(actual: unknown): {
    toBe(expected: unknown): void
    toEqual(expected: unknown): void
    toHaveLength(expected: number): void
    toMatchObject(expected: object): void
  }
}
