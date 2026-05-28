# Avatar + expressiveness

Open items in the avatar / expressiveness depth pass. Shipped entries (B1, B2, B4, B5, B6) live in [`shipped.md`](shipped.md).

---

## B3. Blink-rate modulation by arousal (deferred follow-up to B1)

**Why deferred.** B1's plan considered tying blink interval to the
arousal axis (faster blinks under high arousal, slower under low),
but pixi-live2d-display does not expose a public
`setBlinkingInterval` setter — only the `beforeModelUpdate` event
hook is documented. Overriding the auto-blink driver from
`tickPreModel` every frame would conflict with the existing wink
gesture and is brittle when the library upgrades. Held until we either
swap blink drivers or upstream a setter.

**Sketched approach (when we revisit).**
- Replace the auto blink driver with a custom one that exposes a
  setter; or fork `EyeBlink.update` and own the parameters via
  `tickPreModel`.
- Map arousal -> blink-interval multiplier (e.g. 0.7-1.4 around the
  rig's authored mean) plus a small jitter so the cadence doesn't
  read as metronomic.
- Reuse the `avatar.expressiveness` slider so the user can dampen the
  blink modulation along with the rest of the body-language overlays.

**Open questions.**
- Is the cleanest path forking the `EyeBlink` controller or upstreaming
  a setter PR? The fork is faster, but means we own that surface
  forever.

---

_All open avatar items have shipped — see B4 in [`shipped.md`](shipped.md) for the Phase 5 close-out (new reactions, persona idiom fix, tail-wag breath boost, ear-wiggle override). B3 (blink-rate modulation by arousal) remains the one open follow-up._
