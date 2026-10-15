/** Whether a chosen file is a PDF, by its name; anything else the app accepts is plain text. */
export function isPdf(file: { name: string }): boolean {
  return /\.pdf$/i.test(file.name);
}
