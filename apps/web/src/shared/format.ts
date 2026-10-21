/* how the app writes dates, times and money: english (singapore) formatting throughout. */

const LOCALE = 'en-SG';

/* dollars as the meter shows them: cents, and never "$0.00" for spend that is real but under a cent. */
export function dollars(value: number): string {
  if (value > 0 && value < 0.005) return 'under $0.01';
  return new Intl.NumberFormat(LOCALE, {
    style: 'currency',
    currency: 'USD',
    currencyDisplay: 'narrowSymbol',
  }).format(value);
}

/* when the allowance comes back, in the viewer's local time: today's or tomorrow's date only when it matters. */
export function resetTime(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString(LOCALE, { hour: 'numeric', minute: '2-digit' });
  return at.toDateString() === now.toDateString()
    ? time
    : `${time} on ${at.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short' })}`;
}

/* a day, such as "13 oct 2026". */
export function date(iso: string): string {
  return new Date(iso).toLocaleDateString(LOCALE, { dateStyle: 'medium' });
}

/* a day and time, such as "13 oct 2026, 3:00 pm". */
export function dateTime(iso: string): string {
  return new Date(iso).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' });
}

/* a time of day, such as "3:05 pm". */
export function time(iso: string): string {
  return new Date(iso).toLocaleTimeString(LOCALE, { hour: 'numeric', minute: '2-digit' });
}
