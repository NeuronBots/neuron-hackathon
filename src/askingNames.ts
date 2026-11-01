// "A", "A and B", "A, B, and C"
export function askingNames(people: { name: string }[]): string {
  const names = people.map((person) => person.name)
  if (names.length < 2) return names[0] ?? ""
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`
}
