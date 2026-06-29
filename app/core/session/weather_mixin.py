"""Weather + geocoding plumbing for :class:`SessionController` (H11).

Owns the shared, swappable :class:`app.llm.weather.WeatherProvider` and
:class:`app.llm.weather.Geocoder` that back both H11 consumers — the
passive ambient :class:`~app.core.world.weather_worker.WeatherWorker`
and the on-demand brain tools in :mod:`app.llm.tools.weather`. The two
backends are deliberately decoupled: the weather provider works on
lat/lon only, so swapping it never touches geocoding, and vice versa.

State ownership note: like the other session mixins this class only
groups methods. The single ``__init__``-allocated attribute it relies
on is ``_weather_listeners`` (created in ``SessionController.__init__``);
the provider/geocoder caches and the latest-snapshot memo are created
lazily via ``getattr`` so the controller shell stays uncluttered.

Modeled on :mod:`app.core.session.search_provider_mixin`.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.core.infra import secret_store
from app.core.infra.settings import persist_user_overrides
from app.llm.weather import build_geocoder, build_weather_provider
from app.llm.weather.providers import resolve_api_key


log = logging.getLogger("app.session.weather")


class WeatherMixin:
    """Lazy build + live reconfigure of the shared weather/geocoder pair."""

    # ── provider / geocoder lifecycle ────────────────────────────────

    def _get_weather_provider(self) -> Any:
        """Return the shared weather provider, building it on first use."""
        prov = getattr(self, "_weather_provider", None)
        if prov is None:
            prov = build_weather_provider(getattr(self._settings, "weather", None))
            self._weather_provider = prov
            log.info(
                "weather provider ready: %s",
                getattr(prov, "name", type(prov).__name__),
            )
        return prov

    def _get_geocoder(self) -> Any:
        """Return the shared geocoder, building it on first use."""
        geo = getattr(self, "_weather_geocoder", None)
        if geo is None:
            geo = build_geocoder(getattr(self._settings, "weather", None))
            self._weather_geocoder = geo
        return geo

    def _rebuild_weather_provider(self) -> None:
        """Rebuild the provider + geocoder from current settings."""
        self._weather_provider = build_weather_provider(
            getattr(self._settings, "weather", None)
        )
        self._weather_geocoder = build_geocoder(
            getattr(self._settings, "weather", None)
        )

    # ── home-location helpers ────────────────────────────────────────

    def _weather_home(self) -> "tuple[float, float, str] | None":
        """Return the configured home ``(lat, lon, label)`` or ``None``.

        ``None`` when no coordinates are resolved yet (blank location) —
        the ambient feed stays silent and the brain tool falls back to a
        named-location requirement.
        """
        s = getattr(self._settings, "weather", None)
        if s is None:
            return None
        lat = getattr(s, "latitude", None)
        lon = getattr(s, "longitude", None)
        if lat is None or lon is None:
            return None
        label = (getattr(s, "location_name", "") or "").strip()
        return (float(lat), float(lon), label)

    def _weather_units(self) -> str:
        s = getattr(self._settings, "weather", None)
        return str(getattr(s, "units", "metric") or "metric")

    # ── snapshot listeners + cache ───────────────────────────────────

    def add_weather_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Register a ``callback(snapshot)`` invoked after every fetch.

        The WS bridge translates each snapshot into a ``weather_updated``
        event. Listeners run synchronously on the fetching (worker) thread.
        """
        listeners = getattr(self, "_weather_listeners", None)
        if listeners is None:
            listeners = []
            self._weather_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_weather(self, snapshot: dict[str, Any]) -> None:
        # Cache the latest snapshot for cheap REST / hello reads.
        self._weather_snapshot_cache = dict(snapshot)
        for listener in list(getattr(self, "_weather_listeners", []) or []):
            try:
                listener(snapshot)
            except Exception:
                log.debug("weather listener raised", exc_info=True)

    def weather_snapshot(self) -> "dict[str, Any] | None":
        """Return the latest cached weather snapshot, or ``None``."""
        cache = getattr(self, "_weather_snapshot_cache", None)
        if cache is not None:
            return dict(cache)
        # Fall back to whatever the worker last persisted to kv_meta.
        try:
            from app.core.world.weather_worker import KV_WEATHER_SNAPSHOT

            raw = self._chat_db.kv_get(KV_WEATHER_SNAPSHOT)
            if raw:
                import json

                return json.loads(raw)
        except Exception:
            log.debug("weather_snapshot kv read failed", exc_info=True)
        return None

    def fetch_weather_now(self) -> "dict[str, Any] | None":
        """Force an immediate home-location fetch (best-effort).

        Used by ``reconfigure_weather`` after the location changes and by
        the MCP ``force_weather_fetch`` debug tool. Returns the snapshot
        dict, or ``None`` when no home is configured / the fetch failed.
        """
        home = self._weather_home()
        if home is None:
            return None
        lat, lon, label = home
        try:
            snap = self._get_weather_provider().current(
                lat, lon, units=self._weather_units(), location_label=label,
            )
        except Exception:
            log.debug("fetch_weather_now failed", exc_info=True)
            return None
        blob = snap.to_dict()
        try:
            from app.core.world.weather_worker import persist_weather_snapshot

            persist_weather_snapshot(self._chat_db, blob)
        except Exception:
            log.debug("fetch_weather_now persist failed", exc_info=True)
        self._notify_weather(blob)
        return blob

    # ── seasonal decor (H11 phase 4) ─────────────────────────────────

    def _apply_weather_seasonal_decor(self, snapshot: dict[str, Any]) -> None:
        """Idempotently mirror the real-world sky in Aiko's room.

        Drives two pieces of decor from the home-location snapshot:

        - a ``winter_extra_blanket`` on the bed when it's cold / snowing,
        - an ``open_window`` accent at the window seat when it's a clear,
          warm daytime.

        Idempotent via the ``aiko.seasonal_decor_applied`` kv watermark
        (a small signature of the two booleans) so a steady sky doesn't
        churn the world every fetch. Items are added/removed through the
        :class:`WorldStore` **directly** (not ``add_world_item`` /
        ``delete_world_item``) so this passive feed never stamps the
        intentional-placement hold the user/Aiko set by hand; the
        resulting rows still fan out over the ``world_updated`` bridge
        via :meth:`_notify_world`. Best-effort — any failure is logged
        and skipped.
        """
        store = getattr(self, "_world_store", None)
        if store is None:
            return
        try:
            condition = str(snapshot.get("condition") or "").strip().lower()
            units = str(snapshot.get("units") or "metric").strip().lower()
            temp = float(snapshot.get("temperature") or 0.0)
            is_day = bool(snapshot.get("is_day", True))
            # Normalize to Celsius for the thresholds.
            temp_c = (temp - 32.0) * 5.0 / 9.0 if units == "imperial" else temp
            want_blanket = condition == "snow" or temp_c <= 5.0
            want_open_window = (
                condition == "clear" and is_day and temp_c >= 18.0
            )

            signature = f"blanket={int(want_blanket)};window={int(want_open_window)}"
            try:
                prior = self._chat_db.kv_get("aiko.seasonal_decor_applied")
            except Exception:
                prior = None
            if prior == signature:
                return

            changed = False
            changed |= self._toggle_decor_item(
                store,
                slug="winter_extra_blanket",
                name="extra blanket",
                description=(
                    "a heavy knitted blanket pulled out for the cold"
                ),
                location_slug="bed",
                present=want_blanket,
            )
            changed |= self._toggle_decor_item(
                store,
                slug="open_window",
                name="open window",
                description=(
                    "the window cracked open to let the warm air in"
                ),
                location_slug="window_seat",
                present=want_open_window,
            )

            try:
                self._chat_db.kv_set(
                    "aiko.seasonal_decor_applied", signature,
                )
            except Exception:
                log.debug("seasonal decor watermark write failed", exc_info=True)

            # Optional outfit nudge: on a cold / snowy sky lean toward
            # pajamas. ``_emit_avatar_outfit`` already yields to a
            # user-forced outfit (mode != "auto") and no-ops when she's
            # already in that outfit, so this never fights the user.
            if want_blanket:
                emit = getattr(self, "_emit_avatar_outfit", None)
                if callable(emit):
                    try:
                        emit("pajamas")
                    except Exception:
                        log.debug("weather outfit nudge failed", exc_info=True)

            if changed:
                log.info(
                    "weather seasonal decor: blanket=%s open_window=%s "
                    "condition=%s temp_c=%.1f",
                    want_blanket, want_open_window, condition, temp_c,
                )
        except Exception:
            log.debug("seasonal decor apply failed", exc_info=True)

    def _toggle_decor_item(
        self,
        store: Any,
        *,
        slug: str,
        name: str,
        description: str,
        location_slug: str,
        present: bool,
    ) -> bool:
        """Add or remove one decor item by slug. Returns True if changed."""
        existing = None
        try:
            existing = store.find_item(slug)
            # ``find_item`` is fuzzy; only treat an exact slug hit as ours.
            if existing is not None and existing.slug != slug:
                existing = None
        except Exception:
            existing = None

        if present and existing is None:
            loc = None
            try:
                loc = store.get_location(location_slug)
            except Exception:
                loc = None
            result = store.add_item(
                name=name,
                kind="decor",
                slug=slug,
                description=description,
                location_id=(loc.id if loc is not None else None),
                given_by="weather",
            )
            if result is not None:
                item, _created = result
                try:
                    self._notify_world({"item": item.to_dict()})
                except Exception:
                    log.debug("decor notify (add) failed", exc_info=True)
                return True
            return False

        if not present and existing is not None:
            if store.remove_item(existing.id):
                try:
                    self._notify_world({"deleted_item_id": int(existing.id)})
                except Exception:
                    log.debug("decor notify (remove) failed", exc_info=True)
                return True
            return False

        return False

    # ── REST surface ─────────────────────────────────────────────────

    def _weather_public_snapshot(self) -> dict[str, Any]:
        """Masked settings snapshot for ``GET /api/settings`` (no raw key)."""
        s = getattr(self._settings, "weather", None)
        agent = getattr(self._settings, "agent", None)
        sync_on = bool(getattr(agent, "weather_sync_enabled", False))
        if s is None:
            return {"sync_enabled": sync_on, "location_name": "", "units": "metric"}
        resolved = resolve_api_key(
            getattr(s, "api_key", "") or "",
            getattr(s, "api_key_env", "") or "",
        )
        return {
            "sync_enabled": sync_on,
            "provider": getattr(s, "provider", "open_meteo"),
            "geocoder": getattr(s, "geocoder", "open_meteo"),
            "location_name": getattr(s, "location_name", ""),
            "latitude": getattr(s, "latitude", None),
            "longitude": getattr(s, "longitude", None),
            "units": getattr(s, "units", "metric"),
            "refresh_interval_minutes": int(
                getattr(s, "refresh_interval_minutes", 30)
            ),
            "has_api_key": bool(resolved),
            "api_key_env": getattr(s, "api_key_env", ""),
            "current": self.weather_snapshot(),
        }

    def reconfigure_weather(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial ``weather`` patch, geocode, persist, rebuild.

        Accepts any subset of :class:`WeatherSettings` fields plus a
        top-level ``sync_enabled`` (mapped onto
        ``agent.weather_sync_enabled``). Setting ``location_name`` to a
        new value geocodes it once (via the decoupled geocoder) and
        caches the resulting ``latitude`` / ``longitude``. Returns the
        masked snapshot.
        """
        s = self._settings.weather
        geocode_needed = False

        if "sync_enabled" in patch:
            self._settings.agent.weather_sync_enabled = bool(
                patch["sync_enabled"]
            )
        if "provider" in patch:
            s.provider = str(patch["provider"] or "open_meteo").strip().lower() or "open_meteo"
        if "geocoder" in patch:
            s.geocoder = str(patch["geocoder"] or "open_meteo").strip().lower() or "open_meteo"
        if "units" in patch:
            raw = str(patch["units"] or "metric").strip().lower()
            s.units = raw if raw in ("metric", "imperial") else "metric"
        if "refresh_interval_minutes" in patch:
            try:
                s.refresh_interval_minutes = max(
                    15, int(patch["refresh_interval_minutes"])
                )
            except (TypeError, ValueError):
                pass
        if "api_key" in patch:
            s.api_key = str(patch["api_key"] or "").strip()
        if "api_key_env" in patch:
            s.api_key_env = str(patch["api_key_env"] or "").strip()
        if "location_name" in patch:
            new_name = str(patch["location_name"] or "").strip()[:80]
            if new_name != (s.location_name or ""):
                geocode_needed = True
            s.location_name = new_name
            if not new_name:
                s.latitude = None
                s.longitude = None
        # Explicit coordinates override geocoding.
        if "latitude" in patch and "longitude" in patch:
            try:
                lat = float(patch["latitude"])
                lon = float(patch["longitude"])
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    s.latitude = lat
                    s.longitude = lon
                    geocode_needed = False
            except (TypeError, ValueError):
                pass

        # Rebuild against the (possibly new) provider/geocoder/timeout.
        self._rebuild_weather_provider()

        if geocode_needed and s.location_name:
            try:
                place = self._get_geocoder().resolve(s.location_name)
            except Exception:
                place = None
                log.debug("weather geocode failed", exc_info=True)
            if place is not None:
                s.latitude = place.latitude
                s.longitude = place.longitude
                # Prefer the resolver's canonical label when we have one.
                if place.label:
                    s.location_name = place.label[:80]

        try:
            persist_user_overrides({
                "agent": {
                    "weather_sync_enabled": bool(
                        self._settings.agent.weather_sync_enabled
                    ),
                },
                "weather": {
                    "provider": s.provider,
                    "geocoder": s.geocoder,
                    "location_name": s.location_name,
                    "latitude": s.latitude,
                    "longitude": s.longitude,
                    "units": s.units,
                    "refresh_interval_minutes": s.refresh_interval_minutes,
                    "api_key": secret_store.store_or_passthrough(
                        secret_store.WEATHER_API_KEY_ACCOUNT, s.api_key
                    ),
                    "api_key_env": s.api_key_env,
                },
            })
        except Exception:
            log.warning("persist weather overrides failed", exc_info=True)

        # Kick off an immediate fetch so the UI reflects the new location
        # without waiting for the next idle window.
        try:
            self.fetch_weather_now()
        except Exception:
            log.debug("post-reconfigure weather fetch failed", exc_info=True)

        return self._weather_public_snapshot()
