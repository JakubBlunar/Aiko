"""Long absences allow home visits without breaking shared-world ownership."""
from datetime import timedelta
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.world import in_progress_beat
from app.core.world.garden_visit_worker import GardenVisitWorker
from app.core.world.world_mutation_guard import WorldMutationGuard
from test_garden_visit_worker import _TempWorld
from test_plant_growth_worker import _age_plant
from app.core.world.plant_growth_worker import PlantGrowthWorker


def worker(store, kv=None, idle=3 * 3600, shared=False, asleep=False):
    kv = {} if kv is None else kv
    guard = WorldMutationGuard(
        kv_get=kv.get, intentional_hold_seconds=1800,
        situation_snapshot_provider=lambda: SimpleNamespace(shared_commitment_active=shared),
        sleep_state_provider=lambda: "asleep" if asleep else "awake",
    )
    return GardenVisitWorker(
        store, kv_get=kv.get, kv_set=kv.__setitem__, mutation_guard=guard,
        idle_seconds_provider=lambda: idle, circadian_period_provider=lambda: "morning",
        relax_ratio=0,
    )


def test_long_absence_allows_a_home_garden_visit_from_an_authored_scene():
    with _TempWorld() as store:
        scene = store.add_scene(name="Jacob's room")
        store.travel_to_scene(scene.id)
        home = store.home_scene()
        garden = store.get_location("garden", scene_id=home.id)
        visit = worker(store)
        assert visit.is_ready(now=timephrase.utcnow(), last_run_at=None)
        assert visit.run()["phase"] == "outbound"
        assert store.current_scene_id() == home.id
        assert store.get_state().location_id == garden.id
        # The visit owns its return; she settles in her actual home scene.
        visit._save_return_at(timephrase.utcnow() - timedelta(minutes=1))
        assert visit.run()["phase"] == "inbound"
        assert store.current_scene_id() == home.id
        assert store.get_state().activity != "napping"


def test_short_absence_shared_activity_sleep_and_placement_keep_her_here():
    for options in ({"idle": 60}, {"shared": True}, {"asleep": True}, {}):
        with _TempWorld() as store:
            scene = store.add_scene(name="Jacob's room")
            store.travel_to_scene(scene.id)
            kv = {} if options else {"world.intentional_state_at": timephrase.utcnow().isoformat()}
            visit = worker(store, kv, **options)
            assert not visit.is_ready(now=timephrase.utcnow(), last_run_at=None)
            assert visit.run()["skipped"]
            assert store.current_scene_id() == scene.id


def test_garden_does_not_overwrite_an_open_away_activity():
    with _TempWorld() as store:
        kv = {}
        beat = in_progress_beat.build(key="writing", activity="writing", posture="sitting",
                                     summary="worked on a poem", now=timephrase.utcnow())
        in_progress_beat.save(kv.__setitem__, beat)
        visit = worker(store, kv)
        assert not visit.is_ready(now=timephrase.utcnow(), last_run_at=None)
        assert visit.run()["reason"] == "activity_in_progress"


def test_plants_already_grow_across_scenes_without_moving_aiko():
    with _TempWorld() as store:
        plant = store.list_items(kind="plant")[0]
        _age_plant(store, plant.id, hours_ago=200)
        scene = store.add_scene(name="Jacob's room")
        store.travel_to_scene(scene.id)
        assert PlantGrowthWorker(store).run()["promoted"] > 0
        assert store.current_scene_id() == scene.id
