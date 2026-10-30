"""Adversarial safety checks for re-pinning a dangling embedding binding.

``_repin_dangling_binding`` is the one place where a stored knowledge base can
be pointed at a *different* catalog entry without asking the user. Getting it
wrong silently mixes incompatible vector spaces, and no later check can detect
the damage -- retrieval simply degrades. Every test in this file therefore
attacks the refusal paths: each one must leave the entry byte-for-byte
unchanged.

These cases are deliberately independent of the happy-path tests in
``test_embedding_binding.py`` so that a change to that module's fixtures cannot
weaken them.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deeptutor.services.embedding.config import get_embedding_config
from deeptutor.services.rag import embedding_binding as binding
from deeptutor.services.rag.embedding_signature import signature_from_config
from deeptutor.services.rag.index_versioning import EmbeddingSignature

IDENTITY_FIELDS = ("embedding_signature", "embedding_model", "embedding_dim")


def _catalog_doc() -> dict:
    return {
        "version": 1,
        "services": {
            "embedding": {
                "active_profile_id": "p1",
                "active_model_id": "m1",
                "profiles": [
                    {
                        "id": "p1",
                        "name": "Provider",
                        "binding": "openai",
                        "api_key": "original-key",
                        "base_url": "https://example.test/v1/embeddings",
                        "models": [
                            {"id": "m1", "model": "embed-a", "dimension": "8"},
                            {"id": "m2", "model": "embed-b", "dimension": "16"},
                        ],
                    }
                ],
            }
        },
    }


def _patch_catalog(monkeypatch, value: dict) -> None:
    from deeptutor.services import config as config_module
    from deeptutor.services.config import provider_runtime

    monkeypatch.setattr(
        config_module,
        "get_model_catalog_service",
        lambda: SimpleNamespace(load=lambda: deepcopy(value)),
    )
    monkeypatch.setattr(binding, "load_catalog", lambda: deepcopy(value))
    monkeypatch.setattr(
        provider_runtime,
        "_load_catalog",
        lambda supplied: supplied if supplied is not None else deepcopy(value),
    )


@pytest.fixture
def catalog(monkeypatch):
    value = _catalog_doc()
    _patch_catalog(monkeypatch, value)
    return value


def _selection(model_id: str = "m1", profile_id: str = "p1") -> dict:
    return {"profile_id": profile_id, "model_id": model_id}


def _bound_entry(model_id: str = "m1", **overrides) -> dict:
    """An entry as ``persist_binding`` would have written it."""
    config = get_embedding_config(_selection(model_id))
    return {
        "path": "kb",
        "rag_provider": "llamaindex",
        "status": "ready",
        **binding.binding_fields(_selection(model_id), config),
        **overrides,
    }


def _identity(entry: dict) -> dict:
    return {key: entry.get(key) for key in IDENTITY_FIELDS}


def _renumber_catalog(catalog: dict, profile_id="p9", model_ids=("m9", "m10")) -> dict:
    """Simulate a catalog rebuild: same connections, fresh catalog IDs."""
    service = catalog["services"]["embedding"]
    profile = service["profiles"][0]
    profile["id"] = profile_id
    for model, model_id in zip(profile["models"], model_ids):
        model["id"] = model_id
    service["active_profile_id"] = profile_id
    service["active_model_id"] = model_ids[0]
    return _selection(model_ids[0], profile_id)


# --------------------------------------------------------------------------
# The repair itself, and what it must not touch.
# --------------------------------------------------------------------------


def test_repin_moves_the_catalog_ids_and_nothing_else(catalog):
    entry = _bound_entry()
    identity = _identity(entry)
    new_selection = _renumber_catalog(catalog)
    assert binding.binding_status(entry)[0] == "missing"

    assert binding.migrate_binding(entry, Path("kb")) is True

    assert entry["embedding_selection"] == new_selection
    # Re-pointing IDs is a rename, not a new vector space: re-writing any of
    # these three numbers would force a full rebuild for nothing.
    assert _identity(entry) == identity
    assert binding.binding_status(entry)[0] == "ready"

    # Idempotent: a healthy binding is never examined twice.
    settled = deepcopy(entry)
    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == settled


def test_repin_preserves_fields_it_does_not_understand(catalog):
    """Forward compatibility: unknown keys survive a repair."""
    entry = _bound_entry()
    entry["custom_future_field"] = {"keep": "me"}
    entry["index_versions"] = [{"version": "version-1"}]
    _renumber_catalog(catalog)

    assert binding.migrate_binding(entry, Path("kb")) is True

    assert entry["custom_future_field"] == {"keep": "me"}
    assert entry["index_versions"] == [{"version": "version-1"}]


def test_repin_does_not_clear_a_pending_reindex(catalog):
    """A rename must not pretend the vectors were rebuilt."""
    entry = _bound_entry(needs_reindex=True)
    _renumber_catalog(catalog)

    assert binding.migrate_binding(entry, Path("kb")) is True

    assert entry["needs_reindex"] is True


def test_repin_does_not_rewrite_an_identity_value_it_did_not_have_to(catalog):
    """Existing index-identity values survive verbatim, type included.

    Catalog rebuilds only move IDs, so the stored identity is authoritative and
    must not be re-serialised from the resolved config: an older or hand-edited
    ``kb_config.json`` may hold ``"8"`` where today's writer emits ``8``, and
    ``embedding_dim`` is compared against ``int(...)`` elsewhere.
    """
    entry = _bound_entry()
    entry["embedding_dim"] = "8"  # serialisation drift from an older writer
    _renumber_catalog(catalog)

    assert binding.migrate_binding(entry, Path("kb")) is True

    assert entry["embedding_dim"] == "8"
    assert isinstance(entry["embedding_dim"], str)


# --------------------------------------------------------------------------
# Refusals: every uncertain case keeps the entry exactly as it was.
# --------------------------------------------------------------------------


def test_repin_refuses_when_one_signature_covers_two_vector_spaces(catalog, monkeypatch):
    """A corrupt catalog cannot make one index identity mean two models.

    Only a hand-edited catalog can claim two *different* (model, dimension)
    pairs share one recorded signature; the hash includes both. Picking either
    candidate would silently mix embeddings, so the guard refuses.
    """
    entry = _bound_entry()
    recorded = entry["embedding_signature"]
    _renumber_catalog(catalog)
    catalog["services"]["embedding"]["profiles"].append(
        {
            "id": "p2",
            "name": "Second connection",
            "binding": "openai",
            "api_key": "other-key",
            "base_url": "https://other.test/v1/embeddings",
            "models": [{"id": "m11", "model": "embed-z", "dimension": "64"}],
        }
    )
    # An unknown recorded dimension filters nothing out, so both candidates
    # reach the identity comparison instead of being dropped earlier.
    entry["embedding_dim"] = 0
    honest = binding.signature_from_config

    def colliding(config):
        resolved = honest(config)
        return SimpleNamespace(hash=lambda: recorded, dimension=resolved.dimension)

    monkeypatch.setattr(binding, "signature_from_config", colliding)
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before
    assert binding.binding_status(entry)[0] == "missing"


def test_repin_refuses_when_the_recorded_vector_space_is_gone(catalog):
    """A genuinely deleted/replaced model stays visible to the user."""
    entry = _bound_entry()
    service = catalog["services"]["embedding"]
    profile = service["profiles"][0]
    profile["id"] = "p9"
    # Reconnected with a different output size: the old space no longer exists.
    profile["models"] = [{"id": "m9", "model": "embed-a", "dimension": "1024"}]
    service["active_profile_id"], service["active_model_id"] = "p9", "m9"
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before
    assert binding.binding_status(entry)[0] == "missing"


def test_repin_refuses_when_the_recorded_dimension_disagrees(catalog):
    """Half-written metadata must not be repaired across spaces."""
    entry = _bound_entry()
    _renumber_catalog(catalog)
    entry["embedding_dim"] = 3072  # contradicts the recorded signature (8)
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before
    assert binding.binding_status(entry)[0] == "missing"


def test_repin_refuses_when_no_signature_was_recorded(catalog):
    entry = _bound_entry()
    _renumber_catalog(catalog)
    entry.pop("embedding_signature")
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before


@pytest.mark.parametrize("placeholder", ["legacy", "llamaindex", "graphrag", "lightrag"])
def test_repin_refuses_a_provider_placeholder_signature(catalog, placeholder):
    """Provider names are not vector-space identities."""
    entry = _bound_entry()
    _renumber_catalog(catalog)
    entry["embedding_signature"] = placeholder
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before


def test_a_changed_binding_is_never_repinned_even_with_the_old_space_available(catalog):
    """Only *dangling* pointers are repairable; a model switch is real.

    This is the sharp edge of the rule: the old vector space still exists in
    another connection, yet the stored selection resolves fine, so the KB is
    reporting "changed" and the user -- not this function -- must decide.
    """
    entry = _bound_entry()
    original_signature = entry["embedding_signature"]
    service = catalog["services"]["embedding"]
    service["profiles"][0]["base_url"] = "https://moved.test/v1/embeddings"
    service["profiles"].append(
        {
            "id": "p2",
            "name": "Old endpoint",
            "binding": "openai",
            "api_key": "original-key",
            "base_url": "https://example.test/v1/embeddings",
            "models": [{"id": "m2", "model": "embed-a", "dimension": "8"}],
        }
    )
    assert binding.binding_status(entry)[0] == "changed"
    # The old space really is still resolvable, so the refusal is deliberate.
    assert (
        signature_from_config(get_embedding_config(_selection("m2", "p2"))).hash()
        == original_signature
    )
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before
    assert binding.binding_status(entry)[0] == "changed"


def test_a_ready_binding_is_never_repinned(catalog):
    """A healthy KB must not be silently re-pointed at another duplicate."""
    entry = _bound_entry()
    duplicate = _renumber_catalog(catalog)
    catalog["services"]["embedding"]["profiles"].append(
        {
            "id": "p2",
            "name": "Duplicate connection",
            "binding": "openai",
            "api_key": "original-key",
            "base_url": "https://example.test/v1/embeddings",
            "models": [{"id": "m10", "model": "embed-a", "dimension": "8"}],
        }
    )
    catalog["services"]["embedding"].update(
        {"active_profile_id": "p2", "active_model_id": "m10"}
    )
    assert binding.binding_status(entry)[0] == "missing"
    before = deepcopy(entry)

    # Now the *original* IDs resolve again; nothing may move any more.
    catalog["services"]["embedding"]["profiles"][0]["id"] = "p1"
    catalog["services"]["embedding"]["profiles"][0]["models"] = [
        {"id": "m1", "model": "embed-a", "dimension": "8"},
        {"id": "m3", "model": "embed-b", "dimension": "16"},
    ]
    assert binding.binding_status(entry)[0] == "ready"
    catalog["services"]["embedding"].update(
        {"active_profile_id": duplicate["profile_id"], "active_model_id": duplicate["model_id"]}
    )

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before


def test_repin_skips_entries_that_do_not_use_a_bound_embedding(catalog):
    """Connected / non-bound providers keep their own resolution path."""
    entry = _bound_entry()
    entry["rag_provider"] = "external_index"
    _renumber_catalog(catalog)
    before = deepcopy(entry)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before


def test_repin_survives_an_unreadable_catalog(catalog, monkeypatch):
    entry = _bound_entry()
    _renumber_catalog(catalog)
    before = deepcopy(entry)

    def explode():
        raise ValueError("catalog on disk is corrupt")

    monkeypatch.setattr(binding, "load_catalog", explode)

    assert binding.migrate_binding(entry, Path("kb")) is False
    assert entry == before


# --------------------------------------------------------------------------
# Duplicate handling and the signature/dimension contract.
# --------------------------------------------------------------------------


def test_repin_prefers_the_active_duplicate_of_the_same_vector_space(catalog):
    """Equivalent duplicates must not make the repair look ambiguous."""
    entry = _bound_entry()
    service = catalog["services"]["embedding"]
    service["profiles"].pop(0)  # the connection the KB was indexed against
    for suffix in ("2", "3"):
        service["profiles"].append(
            {
                "id": f"p{suffix}",
                "name": f"Provider {suffix}",
                "binding": "openai",
                "api_key": "original-key",
                "base_url": "https://example.test/v1/embeddings",
                "models": [{"id": f"m{suffix}", "model": "embed-a", "dimension": "8"}],
            }
        )
    service["active_profile_id"], service["active_model_id"] = "p3", "m3"
    assert binding.binding_status(entry)[0] == "missing"

    assert binding.migrate_binding(entry, Path("kb")) is True

    assert entry["embedding_selection"] == {"profile_id": "p3", "model_id": "m3"}
    assert _identity(entry)["embedding_signature"] == entry["embedding_signature"]


def test_signature_dimension_is_the_same_number_as_config_dim(catalog):
    """The guard compares ``signature.dimension`` with ``config.dim``.

    If those two ever meant different things the dimension check would be
    vacuous (or wrong in the safe direction only by accident).
    """
    assert "dimension" in {field.name for field in fields(EmbeddingSignature)}
    for model_id in ("m1", "m2"):
        config = get_embedding_config(_selection(model_id))
        assert signature_from_config(config).dimension == config.dim
    assert get_embedding_config(_selection("m1")).dim == 8
    assert get_embedding_config(_selection("m2")).dim == 16


def test_repin_result_is_json_serialisable(catalog):
    """The repaired entry is written straight to ``kb_config.json``."""
    entry = _bound_entry()
    _renumber_catalog(catalog)

    assert binding.migrate_binding(entry, Path("kb")) is True
    assert json.loads(json.dumps(entry))["embedding_selection"] == entry["embedding_selection"]
