# Integration and wiring

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

<a id="i9-mobile-responsiveness--pwa-installability"></a>

## I9. Mobile and PWA acceptance checks

**Status: Validation remaining.**

**Implemented / available:** Mobile App/navigation/floating-persona layouts, manifest and production
browser service worker exist. Navigation is network-first; hashed assets cache;
skipWaiting/clients.claim are implemented; API/private paths bypass caching.

**Remaining:** Verify installed mobile/HTTPS update, offline fallback, keyboard/safe-area and voice
behavior on target devices. Source/config and layout-state tests are not device E2E proof. Do not
rebuild the mobile/PWA foundation.

[Implemented slice](shipped/audit-2026-10-24.md#integration-i9).

**Source check:** [App.tsx](../../web/src/App.tsx),
[manifest.webmanifest](../../web/public/manifest.webmanifest), [sw.js](../../web/public/sw.js),
[main.tsx](../../web/src/main.tsx), [store.layout.test.ts](../../web/src/store.layout.test.ts),
[runtime.ts](../../web/src/desktop/runtime.ts).

## I11. Graduate a corroborated belief into a concept

**Status: Open implementation.**

**Implemented / available:** Trusted beliefs, corroboration metadata, concepts and the attributable
belief-outcome ledger exist.

**Remaining:** Define evidence-spanning graduation, post-promotion ownership and
contradiction/retraction routing, then implement belief-to-concept promotion. Mood/repetition
outcomes are not factual accuracy.

**Source check:** [belief_store.py](../../app/core/relationship/belief_store.py),
[belief_worker.py](../../app/core/relationship/belief_worker.py),
[concept_store.py](../../app/core/concepts/concept_store.py).
