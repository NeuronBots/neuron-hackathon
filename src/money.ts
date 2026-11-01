// Amounts are in the smallest unit of their currency (paise, cents)
export function money(amount: number, currency: string): string {
  return new Intl.NumberFormat([], { style: "currency", currency }).format(amount / 100)
}
