from __future__ import annotations

import asyncio

import pytest

from golem.edict import (
    EDICT_DONE,
    EDICT_FAILED,
    EDICT_IN_PROGRESS,
    EDICT_NEEDS_ATTENTION,
    EDICT_PENDING,
    EDICT_PLANNING,
    Edict,
    EdictStore,
)


def _make_edict(**kwargs: object) -> Edict:
    defaults: dict[str, object] = {
        "id": "",
        "repo_path": "/some/repo",
        "title": "Test edict",
        "body": "Do something useful",
    }
    defaults.update(kwargs)
    return Edict(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_assigns_sequential_ids(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    e1 = _make_edict(title="First")
    e2 = _make_edict(title="Second")
    id1 = await store.create(e1)
    id2 = await store.create(e2)
    assert id1 == "EDICT-001"
    assert id2 == "EDICT-002"


@pytest.mark.asyncio
async def test_create_sets_timestamps(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    edict = _make_edict()
    await store.create(edict)
    assert edict.created_at != ""
    assert edict.updated_at != ""
    assert edict.created_at == edict.updated_at


@pytest.mark.asyncio
async def test_create_default_status_is_pending(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    edict = _make_edict()
    eid = await store.create(edict)
    stored = await store.read(eid)
    assert stored.status == EDICT_PENDING


@pytest.mark.asyncio
async def test_read_by_id(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    original = _make_edict(title="Read me", body="body text", repo_path="/repo/x")
    eid = await store.create(original)
    result = await store.read(eid)
    assert result.id == eid
    assert result.title == "Read me"
    assert result.body == "body text"
    assert result.repo_path == "/repo/x"


@pytest.mark.asyncio
async def test_read_case_insensitive(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    edict = _make_edict()
    eid = await store.create(edict)
    result_lower = await store.read(eid.lower())
    assert result_lower.id == eid


@pytest.mark.asyncio
async def test_update_status_pending_to_planning(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    result = await store.read(eid)
    assert result.status == EDICT_PLANNING


@pytest.mark.asyncio
async def test_update_status_planning_to_in_progress(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_IN_PROGRESS)
    result = await store.read(eid)
    assert result.status == EDICT_IN_PROGRESS


@pytest.mark.asyncio
async def test_update_status_in_progress_to_done(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_IN_PROGRESS)
    await store.update_status(eid, EDICT_DONE)
    result = await store.read(eid)
    assert result.status == EDICT_DONE


@pytest.mark.asyncio
async def test_update_status_in_progress_to_failed(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_IN_PROGRESS)
    await store.update_status(eid, EDICT_FAILED, error="something broke")
    result = await store.read(eid)
    assert result.status == EDICT_FAILED
    assert result.error == "something broke"


@pytest.mark.asyncio
async def test_update_status_needs_attention_to_planning(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_NEEDS_ATTENTION)
    await store.update_status(eid, EDICT_PLANNING)
    result = await store.read(eid)
    assert result.status == EDICT_PLANNING


@pytest.mark.asyncio
async def test_invalid_transition_raises_value_error(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    # pending -> in_progress is not allowed (must go through planning first)
    with pytest.raises(ValueError, match="Invalid transition"):
        await store.update_status(eid, EDICT_IN_PROGRESS)


@pytest.mark.asyncio
async def test_invalid_transition_done_to_pending(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_IN_PROGRESS)
    await store.update_status(eid, EDICT_DONE)
    # done has no outbound transitions
    with pytest.raises(ValueError, match="Invalid transition"):
        await store.update_status(eid, EDICT_PENDING)


@pytest.mark.asyncio
async def test_invalid_transition_failed_to_pending(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_FAILED)
    with pytest.raises(ValueError, match="Invalid transition"):
        await store.update_status(eid, EDICT_PENDING)


@pytest.mark.asyncio
async def test_update_fields(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update(eid, title="Updated title", body="New body", pr_url="https://github.com/pr/1")
    result = await store.read(eid)
    assert result.title == "Updated title"
    assert result.body == "New body"
    assert result.pr_url == "https://github.com/pr/1"


@pytest.mark.asyncio
async def test_update_ticket_ids_and_cost(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update(eid, ticket_ids=["TICKET-001", "TICKET-002"], cost_usd=1.25)
    result = await store.read(eid)
    assert result.ticket_ids == ["TICKET-001", "TICKET-002"]
    assert result.cost_usd == 1.25


@pytest.mark.asyncio
async def test_update_refreshes_updated_at(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    original_updated_at = (await store.read(eid)).updated_at
    # Small sleep to ensure timestamp differs
    await asyncio.sleep(0.01)
    await store.update(eid, title="Changed")
    result = await store.read(eid)
    assert result.updated_at >= original_updated_at


@pytest.mark.asyncio
async def test_list_edicts_no_filter(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    await store.create(_make_edict(title="A"))
    await store.create(_make_edict(title="B"))
    await store.create(_make_edict(title="C"))
    all_edicts = await store.list_edicts()
    assert len(all_edicts) == 3
    assert [e.id for e in all_edicts] == ["EDICT-001", "EDICT-002", "EDICT-003"]


@pytest.mark.asyncio
async def test_list_edicts_status_filter(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    e1_id = await store.create(_make_edict(title="A"))
    e2_id = await store.create(_make_edict(title="B"))
    await store.create(_make_edict(title="C"))
    await store.update_status(e1_id, EDICT_PLANNING)
    await store.update_status(e2_id, EDICT_PLANNING)
    planning = await store.list_edicts(status_filter=EDICT_PLANNING)
    assert len(planning) == 2
    pending = await store.list_edicts(status_filter=EDICT_PENDING)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_list_edicts_empty_dir(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    result = await store.list_edicts()
    assert result == []


@pytest.mark.asyncio
async def test_delete_edict(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    deleted = await store.delete(eid)
    assert deleted is True
    remaining = await store.list_edicts()
    assert remaining == []


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    result = await store.delete("EDICT-999")
    assert result is False


@pytest.mark.asyncio
async def test_concurrent_create_no_id_collision(tmp_path):
    store = EdictStore(tmp_path / "edicts")

    async def create_one(title: str) -> str:
        return await store.create(_make_edict(title=title))

    ids = await asyncio.gather(
        create_one("Concurrent A"),
        create_one("Concurrent B"),
        create_one("Concurrent C"),
    )
    unique_ids = set(ids)
    assert len(unique_ids) == 3
    assert "EDICT-001" in unique_ids
    assert "EDICT-002" in unique_ids
    assert "EDICT-003" in unique_ids


@pytest.mark.asyncio
async def test_roundtrip_serialization_all_fields(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    original = Edict(
        id="",
        repo_path="/my/repo",
        title="Full roundtrip",
        body="Detailed spec body",
        pr_url="https://github.com/pr/42",
        ticket_ids=["TICKET-001", "TICKET-002"],
        cost_usd=3.14,
        error=None,
    )
    eid = await store.create(original)
    result = await store.read(eid)
    assert result.id == eid
    assert result.repo_path == "/my/repo"
    assert result.title == "Full roundtrip"
    assert result.body == "Detailed spec body"
    assert result.status == EDICT_PENDING
    assert result.pr_url == "https://github.com/pr/42"
    assert result.ticket_ids == ["TICKET-001", "TICKET-002"]
    assert result.cost_usd == 3.14
    assert result.error is None
    assert result.created_at != ""
    assert result.updated_at != ""


@pytest.mark.asyncio
async def test_roundtrip_with_error_field(tmp_path):
    store = EdictStore(tmp_path / "edicts")
    eid = await store.create(_make_edict())
    await store.update_status(eid, EDICT_PLANNING)
    await store.update_status(eid, EDICT_IN_PROGRESS)
    await store.update_status(eid, EDICT_FAILED, error="agent timed out")
    result = await store.read(eid)
    assert result.error == "agent timed out"
    assert result.status == EDICT_FAILED
