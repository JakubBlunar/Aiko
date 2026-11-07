/** Leave the avatar's own width untouched and preserve a usable chat column. */
export function workCanDock(viewport: number, sidebarCollapsed: boolean, personaVisible: boolean,
  personaWidth: number, workWidth: number) {
  return viewport >= 768 && viewport >= (sidebarCollapsed ? 64 : 256) +
    (!personaVisible && viewport >= 1024 ? personaWidth + 6 : 0) + workWidth + 420;
}
