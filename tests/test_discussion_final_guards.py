from __future__ import annotations

import pytest

from astrbot_plugin_dynamics_horizons.core.feedback import enqueue, process_jobs

from test_discussion_feedback import (
    LOG_ID,
    REFLECTION,
    USER_TEXT,
    Model,
    Rig,
    _count_reflections,
    _delivery_event,
    _incoming,
    _model,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("reflection", {"text": REFLECTION}),
        ("reflection", [REFLECTION]),
        ("anchor_event_id", ["incorrectly-typed-anchor"]),
        ("log_id", [LOG_ID]),
    ],
)
async def test_malformed_model_fields_do_not_create_reflections(tmp_path, field, malformed):
    model = _model()
    model.outputs[1]["reflections"][0][field] = malformed
    rig = Rig(tmp_path / "malformed.sqlite3", model)
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        await process_jobs(rig.engine)

        assert len(model.calls) == 2
        assert _count_reflections(rig) == 0
        job = rig.store.db.execute(
            "SELECT status FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()
        assert job["status"] == "done"
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_note_and_completed_job_roll_back_together_and_retry_once(tmp_path):
    model = _model()
    rig = Rig(tmp_path / "atomic.sqlite3", model)
    try:
        assert enqueue(rig.engine, _delivery_event())["queued"]
        rig.tick()
        assert enqueue(rig.engine, _incoming())["queued"]
        # Abort precisely at the final job completion update, after the note's
        # INSERT. The ordinary retry handler should then leave the job pending.
        with rig.store.lock, rig.store.db:
            rig.store.db.execute(
                """CREATE TRIGGER fail_discussion_completion
                BEFORE UPDATE OF status ON feedback_jobs
                WHEN NEW.status='done'
                 AND json_extract(OLD.payload_json, '$.kind')='discussion_reply'
                BEGIN SELECT RAISE(ABORT, 'injected_completion_failure'); END"""
            )

        await process_jobs(rig.engine)
        failed = rig.store.db.execute(
            "SELECT status,attempts,next_at FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()
        assert len(model.calls) == 2
        assert _count_reflections(rig) == 0
        assert failed["status"] == "pending"
        assert failed["attempts"] == 1

        with rig.store.lock, rig.store.db:
            rig.store.db.execute("DROP TRIGGER fail_discussion_completion")
        model.outputs.append(
            {
                "reflections": [
                    {
                        "anchor_event_id": "",
                        "log_id": LOG_ID,
                        "member_quote": USER_TEXT[:16],
                        "reflection": REFLECTION,
                    }
                ]
            }
        )
        rig.clock_value = failed["next_at"] + 1
        await process_jobs(rig.engine)
        await process_jobs(rig.engine)

        completed = rig.store.db.execute(
            "SELECT status,attempts FROM feedback_jobs WHERE json_extract(payload_json,'$.kind')='discussion_reply'"
        ).fetchone()
        assert _count_reflections(rig) == 1
        assert completed["status"] == "done"
        assert completed["attempts"] == 1
    finally:
        rig.close()
