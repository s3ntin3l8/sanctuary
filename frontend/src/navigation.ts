/**
 * Leave the SPA for a server-chosen path with a full page load. Used where the
 * destination may be a page that is still server-rendered.
 */
export function leaveTo(path: string) {
  window.location.assign(path)
}
