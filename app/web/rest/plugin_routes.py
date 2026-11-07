"""Schema-driven plugin settings; capability-specific knowledge stays in bundles."""

from fastapi import HTTPException

from app.plugins.manager import PluginConflict, PluginValidation


def register(app, session, hub=None):
    if hub is not None and getattr(session, "plugins", None) is not None:
        session.plugins.add_listener(
            lambda event: hub.broadcast({"type": "plugins_updated", **event})
        )

    def runtime():
        value = getattr(session, "plugins", None)
        if value is None:
            raise HTTPException(503, "Plugin runtime is unavailable")
        return value

    def invoke(operation):
        try:
            return operation()
        except PluginConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PluginValidation as exc:
            raise HTTPException(422, exc.errors) from None
        except KeyError:
            raise HTTPException(404, "Unknown plugin") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/plugins")
    def list_plugins():
        return runtime().snapshot()

    @app.patch("/api/plugins")
    def set_plugins(payload: dict):
        return invoke(lambda: runtime().set_master(payload.get("enabled")))

    @app.patch("/api/plugins/{plugin_id}")
    def update_plugin(plugin_id: str, payload: dict):
        if set(payload) - {"revision", "config", "enabled", "reset"}:
            raise HTTPException(422, "Unknown plugin update field")
        if "reset" in payload and type(payload["reset"]) is not bool:
            raise HTTPException(422, "reset must be a boolean")
        if "enabled" in payload and type(payload["enabled"]) is not bool:
            raise HTTPException(422, "enabled must be a boolean")
        if "config" in payload and not isinstance(payload["config"], dict):
            raise HTTPException(422, "config must be an object")
        if payload.get("reset") and "config" in payload:
            raise HTTPException(422, "Reset overrides or edit settings in one request")
        return invoke(
            lambda: runtime().update(
                plugin_id,
                expected_revision=payload.get("revision"),
                config=payload.get("config"),
                enabled=payload.get("enabled"),
                reset=payload.get("reset", False),
            )
        )

    @app.post("/api/plugins/{plugin_id}/retry")
    def retry_plugin(plugin_id: str, payload: dict):
        return invoke(lambda: runtime().retry(plugin_id, payload.get("revision")))
