/** Activity models are pure functions, so they cannot use React's `t` hook. */
export function localizeActivityText(value: string): string {
  // Streaming activity is a task-progress surface, not the general WebUI locale.
  // Keep its canonical Chinese copy stable so a browser-language switch cannot
  // turn one running task into a mixture of Chinese and English status rows.
  return value;
}
