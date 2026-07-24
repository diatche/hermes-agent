export function olderHalf<T>(items: readonly T[]): T[] {
  return items.slice(Math.ceil(items.length / 2))
}

export function olderHalfBy<T>(items: readonly T[], recency: (item: T) => number): T[] {
  const newestFirst = [...items].sort((left, right) => recency(right) - recency(left))

  return olderHalf(newestFirst)
}
