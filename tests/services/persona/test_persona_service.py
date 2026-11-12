"""PersonaService: CRUD, single-persona context rendering, legacy migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeptutor.services.persona.service import (
    PRESETS_DIR,
    InvalidPersonaNameError,
    PersonaExistsError,
    PersonaNotFoundError,
    PersonaService,
    load_visible_for_context,
)


@pytest.fixture
def service(tmp_path: Path) -> PersonaService:
    return PersonaService(root=tmp_path / "personas")


def test_create_and_get(service: PersonaService) -> None:
    service.create("teacher", "Patient tutor", "Explain step by step.")
    detail = service.get_detail("teacher")
    assert detail.name == "teacher"
    assert detail.description == "Patient tutor"
    assert "Explain step by step." in detail.content
    assert detail.source == "user"


def test_create_duplicate_rejected(service: PersonaService) -> None:
    service.create("peer", "A peer", "body")
    with pytest.raises(PersonaExistsError):
        service.create("peer", "again", "body")


def test_invalid_name_rejected(service: PersonaService) -> None:
    with pytest.raises(InvalidPersonaNameError):
        service.create("Has Spaces", "d", "b")


def test_load_for_context_wraps_body(service: PersonaService) -> None:
    service.create("peer", "Study partner", "Think out loud with the user.")
    rendered = service.load_for_context("peer")
    assert "## Active Persona" in rendered
    assert "### Persona: peer" in rendered
    assert "Think out loud with the user." in rendered


def test_load_for_context_missing_is_empty(service: PersonaService) -> None:
    assert service.load_for_context("ghost") == ""
    assert service.load_for_context("") == ""


def test_update_description_and_rename(service: PersonaService) -> None:
    service.create("coach", "old", "body")
    service.update("coach", description="new desc")
    assert service.get_detail("coach").description == "new desc"
    service.update("coach", rename_to="mentor")
    assert service.get_detail("mentor").description == "new desc"
    with pytest.raises(PersonaNotFoundError):
        service.get_detail("coach")


def test_delete(service: PersonaService) -> None:
    service.create("temp", "d", "b")
    service.delete("temp")
    with pytest.raises(PersonaNotFoundError):
        service.get_detail("temp")


def test_list_personas(service: PersonaService) -> None:
    service.create("a", "first", "b")
    service.create("b", "second", "b")
    names = {p.name for p in service.list_personas()}
    assert names == {"a", "b"}


def test_migrate_legacy_skills(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    peer_dir = skills_root / "peer"
    peer_dir.mkdir(parents=True)
    (peer_dir / "SKILL.md").write_text(
        "---\nname: peer\ndescription: Study partner\ntriggers: [discuss]\n---\n\nBe a peer.\n"
    )
    # A non-persona skill must be left untouched.
    other = skills_root / "data-tool"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: data-tool\ndescription: x\n---\n\nbody\n")

    service = PersonaService(root=tmp_path / "personas")
    migrated = service.migrate_legacy_skills(skills_root)

    assert migrated == ["peer"]
    detail = service.get_detail("peer")
    assert detail.description == "Study partner"
    assert "Be a peer." in detail.content
    # legacy frontmatter keys (triggers) are dropped
    assert "triggers" not in detail.content
    # source skill dir removed, unrelated skill preserved
    assert not peer_dir.exists()
    assert (other / "SKILL.md").exists()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    (skills_root / "teacher").mkdir(parents=True)
    (skills_root / "teacher" / "SKILL.md").write_text(
        "---\nname: teacher\ndescription: T\n---\n\nbody\n"
    )
    service = PersonaService(root=tmp_path / "personas")
    assert service.migrate_legacy_skills(skills_root) == ["teacher"]
    # second run finds nothing new
    assert service.migrate_legacy_skills(skills_root) == []


def test_category_survives_update_rewrite(service: PersonaService) -> None:
    """Editing a body rebuilds the frontmatter — category must come back with it."""
    service.create(
        "cv-auditor",
        "Resume auditor",
        "---\nname: cv-auditor\ndescription: d\n"
        "display_name: 简历审计师\ncategory: work\n---\n\nBody",
    )
    current = service.get_detail("cv-auditor").content
    info = service.update("cv-auditor", description="d2", content=current)
    assert info.category == "work"
    assert info.display_name == "简历审计师"
    detail = service.get_detail("cv-auditor")
    assert detail.category == "work"
    assert "category: work" in detail.content
    assert "Body" in detail.content


def test_persona_without_category_is_valid(service: PersonaService) -> None:
    """Personas written before categories existed must keep working."""
    service.create("plain", "d", "Body only")
    [info] = service.list_personas()
    assert info.name == "plain"
    assert info.category == ""


def test_seed_presets_creates_defaults(service: PersonaService) -> None:
    # Issue #659: fresh installs must expose peer / teacher / research-assistant.
    # The bundled preset set grows over time (the expert personas were added
    # later), so the expectation is "every preset on disk", not a fixed list.
    bundled = {p.name for p in PRESETS_DIR.iterdir() if (p / "PERSONA.md").exists()}
    seeded = service.seed_presets()
    assert set(seeded) == bundled
    names = {p.name for p in service.list_personas()}
    assert bundled <= names
    assert {"peer", "teacher", "research-assistant"} <= names
    teacher = service.get_detail("teacher")
    assert teacher.description  # frontmatter description survived
    assert "Teacher Mode" in teacher.content


def test_seed_presets_keeps_categories(service: PersonaService) -> None:
    """The gallery's category tabs read this frontmatter key off the seeded copy."""
    service.seed_presets()
    categories = {p.name: p.category for p in service.list_personas()}
    assert categories["ket-strategist"] == "education"
    assert categories["cv-auditor"] == "work"
    assert categories["software-engineer"] == "code"
    assert categories["teacher"] == "education"


def test_seed_presets_is_idempotent_and_non_destructive(service: PersonaService) -> None:
    service.seed_presets()
    # A user edit to a seeded persona must not be clobbered by a later seed.
    service.update("peer", content="---\nname: peer\ndescription: mine\n---\n\nCustom body.")
    assert service.seed_presets() == []  # nothing re-seeded
    assert "Custom body." in service.get_detail("peer").content


def test_load_visible_for_context_falls_back_to_admin_presets(
    tmp_path: Path,
) -> None:
    # Admin in an explicit workspace: local dir empty, presets live on the
    # account-level admin tree. Selecting one must still inject the body.
    workspace = PersonaService(root=tmp_path / "workspace" / "personas")
    admin = PersonaService(root=tmp_path / "admin" / "personas")
    admin.seed_presets()

    rendered = load_visible_for_context("teacher", workspace=workspace, admin=admin)
    assert "## Active Persona" in rendered
    assert "### Persona: teacher" in rendered
    assert workspace.load_for_context("teacher") == ""


def test_load_visible_for_context_prefers_workspace_shadow(
    tmp_path: Path,
) -> None:
    workspace = PersonaService(root=tmp_path / "workspace" / "personas")
    admin = PersonaService(root=tmp_path / "admin" / "personas")
    admin.seed_presets()
    workspace.create("teacher", "Mine", "Local voice.")

    rendered = load_visible_for_context("teacher", workspace=workspace, admin=admin)
    assert "Local voice." in rendered
    assert "Teacher Mode" not in rendered
