---
name: browser
description: Drive a real browser via accessibility snapshots + refs
---
Browser skills — how to use them well:
- Snapshot FIRST. Call the snapshot skill to get the ranked element list with refs (e.g. e12); prefer it over a screenshot. Pass those refs to click/type.
- Refs go STALE after any navigation, scroll, or DOM change — re-snapshot before reusing refs, never reuse old ones.
- For a big page, scope the snapshot with a CSS `selector` (e.g. "main") to cut noise.
- To act: click/type/press_key/scroll/select using a ref from the LATEST snapshot. Submit with press_key Enter.
- Dropdowns / menus / React portals: click the trigger, wait briefly, then click the option by visible text (a click-by-text skill if available). Avoid the evaluate/JavaScript skill for UI — it breaks on strict-CSP sites and can steal focus.
- After an action, re-snapshot (or find) to confirm the result before the next step.
- Safety: NEVER close tabs you didn't open, and don't navigate away from the page the user is on unless the goal requires it.
