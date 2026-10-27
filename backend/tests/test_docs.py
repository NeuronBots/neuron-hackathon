"""Tests for document store CRUD endpoints and access control (NURN-153)."""
from datetime import datetime, timedelta
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from core.access import COOKIE, hash_password
import core.database as database
from core.models import AuthSession, Doc as DocModel, User
from routers.docs import Doc, DocCreate


PASSWORD = "testpassword123"


@pytest.fixture
def client(db):
    from router import app
    app.dependency_overrides[database.get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(db, name: str, token: str | None = None) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}@example.com",
        name=name,
        password_hash=hash_password(PASSWORD),
    )
    db.add(user)
    db.commit()
    if token:
        db.add(AuthSession(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=7),
        ))
        db.commit()
    return user


def test_post_valid_doc_returns_201(client):
    payload = {
        "title": "Design Specs",
        "content": "Architecture for Meta hackathon work",
        "tags": ["meta", "specs"],
    }
    res = client.post("/api/docs", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["title"] == payload["title"]
    assert data["content"] == payload["content"]
    assert data["tags"] == ["meta", "specs"]
    assert "id" in data and len(data["id"]) > 0
    assert "created_at" in data
    assert "updated_at" in data
    # Verify Pydantic validation
    doc = Doc.model_validate(data)
    assert doc.title == payload["title"]


def test_post_valid_doc_without_tags_defaults_empty_list(client):
    payload = {
        "title": "Minimal Doc",
        "content": "No tags provided",
    }
    res = client.post("/api/docs", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()
    assert data["tags"] == []


def test_post_empty_title_returns_422(client):
    res = client.post("/api/docs", json={"title": "", "content": "Valid content"})
    assert res.status_code == 422

    res = client.post("/api/docs", json={"title": "   ", "content": "Valid content"})
    assert res.status_code == 422

    res = client.post("/api/docs", json={"content": "Valid content"})
    assert res.status_code == 422


def test_post_empty_content_returns_422(client):
    res = client.post("/api/docs", json={"title": "Valid title", "content": ""})
    assert res.status_code == 422

    res = client.post("/api/docs", json={"title": "Valid title", "content": "   "})
    assert res.status_code == 422

    res = client.post("/api/docs", json={"title": "Valid title"})
    assert res.status_code == 422


def test_post_title_too_long_returns_422(client):
    long_title = "x" * 201
    res = client.post("/api/docs", json={"title": long_title, "content": "Valid content"})
    assert res.status_code == 422


def test_get_docs_list_contains_created_doc_ordered_newest_first(db, client):
    user = make_user(db, "alice_order", token="tok_alice_order")
    client.cookies.set(COOKIE, "tok_alice_order")

    res1 = client.post("/api/docs", json={"title": "First Doc", "content": "First content"})
    assert res1.status_code == 201
    doc1 = res1.json()

    res2 = client.post("/api/docs", json={"title": "Second Doc", "content": "Second content"})
    assert res2.status_code == 201
    doc2 = res2.json()

    res_list = client.get("/api/docs")
    assert res_list.status_code == 200
    docs = res_list.json()

    doc_ids = [d["id"] for d in docs]
    assert doc1["id"] in doc_ids
    assert doc2["id"] in doc_ids
    idx1 = doc_ids.index(doc1["id"])
    idx2 = doc_ids.index(doc2["id"])
    assert idx2 < idx1


def test_get_single_doc_by_id(db, client):
    user = make_user(db, "alice_single", token="tok_alice_single")
    client.cookies.set(COOKIE, "tok_alice_single")

    created = client.post("/api/docs", json={"title": "Single Doc", "content": "Single content"}).json()
    doc_id = created["id"]

    res = client.get(f"/api/docs/{doc_id}")
    assert res.status_code == 200
    assert res.json()["id"] == doc_id
    assert res.json()["title"] == "Single Doc"


def test_get_single_doc_404_if_missing(client):
    res = client.get("/api/docs/non-existent-id")
    assert res.status_code == 404


def test_user_can_only_see_own_docs_unless_shared(db, client):
    alice = make_user(db, "alice", token="alice_tok")
    bob = make_user(db, "bob", token="bob_tok")
    charlie = make_user(db, "charlie", token="charlie_tok")

    # Alice creates a private doc
    client.cookies.set(COOKIE, "alice_tok")
    res_alice_doc = client.post("/api/docs", json={"title": "Alice Secret", "content": "For Alice only"})
    assert res_alice_doc.status_code == 201
    alice_doc = res_alice_doc.json()

    # Bob creates a private doc
    client.cookies.set(COOKIE, "bob_tok")
    res_bob_doc = client.post("/api/docs", json={"title": "Bob Secret", "content": "For Bob only"})
    assert res_bob_doc.status_code == 201
    bob_doc = res_bob_doc.json()

    # Alice sees only her doc
    client.cookies.set(COOKIE, "alice_tok")
    res_a_list = client.get("/api/docs")
    a_ids = [d["id"] for d in res_a_list.json()]
    assert alice_doc["id"] in a_ids
    assert bob_doc["id"] not in a_ids

    # Alice tries to get Bob's doc directly -> 404
    assert client.get(f"/api/docs/{bob_doc['id']}").status_code == 404

    # Bob sees only his doc
    client.cookies.set(COOKIE, "bob_tok")
    res_b_list = client.get("/api/docs")
    b_ids = [d["id"] for d in res_b_list.json()]
    assert bob_doc["id"] in b_ids
    assert alice_doc["id"] not in b_ids

    # Bob tries to get Alice's doc directly -> 404
    assert client.get(f"/api/docs/{alice_doc['id']}").status_code == 404

    # Charlie sees neither
    client.cookies.set(COOKIE, "charlie_tok")
    res_c_list = client.get("/api/docs")
    c_ids = [d["id"] for d in res_c_list.json()]
    assert alice_doc["id"] not in c_ids
    assert bob_doc["id"] not in c_ids
    assert client.get(f"/api/docs/{alice_doc['id']}").status_code == 404
    assert client.get(f"/api/docs/{bob_doc['id']}").status_code == 404

    # Alice shares her doc with Bob via POST /docs/{id}/share
    client.cookies.set(COOKIE, "alice_tok")
    res_share = client.post(f"/api/docs/{alice_doc['id']}/share", json={"users": [bob.id]})
    assert res_share.status_code == 200
    assert bob.id in res_share.json()["shared_with"]

    # Now Bob can see Alice's doc in GET /docs and GET /docs/{id}
    client.cookies.set(COOKIE, "bob_tok")
    res_b_list_after = client.get("/api/docs")
    b_ids_after = [d["id"] for d in res_b_list_after.json()]
    assert alice_doc["id"] in b_ids_after
    assert bob_doc["id"] in b_ids_after
    res_b_get_a = client.get(f"/api/docs/{alice_doc['id']}")
    assert res_b_get_a.status_code == 200
    assert res_b_get_a.json()["title"] == "Alice Secret"

    # Charlie still cannot see it
    client.cookies.set(COOKIE, "charlie_tok")
    res_c_list_after = client.get("/api/docs")
    c_ids_after = [d["id"] for d in res_c_list_after.json()]
    assert alice_doc["id"] not in c_ids_after
    assert client.get(f"/api/docs/{alice_doc['id']}").status_code == 404


def test_create_doc_with_shared_with_at_creation(db, client):
    alice = make_user(db, "alice2", token="alice2_tok")
    bob = make_user(db, "bob2", token="bob2_tok")

    # Alice creates a doc pre-shared with Bob using his email
    client.cookies.set(COOKIE, "alice2_tok")
    res = client.post("/api/docs", json={
        "title": "Shared from start",
        "content": "Collaborative content",
        "shared_with": [bob.email],
    })
    assert res.status_code == 201
    doc_id = res.json()["id"]

    # Bob can see it immediately by email identity
    client.cookies.set(COOKIE, "bob2_tok")
    res_b = client.get(f"/api/docs/{doc_id}")
    assert res_b.status_code == 200
    assert res_b.json()["title"] == "Shared from start"


def test_non_creator_cannot_share_doc(db, client):
    alice = make_user(db, "alice3", token="alice3_tok")
    bob = make_user(db, "bob3", token="bob3_tok")
    charlie = make_user(db, "charlie3", token="charlie3_tok")

    client.cookies.set(COOKIE, "alice3_tok")
    res_doc = client.post("/api/docs", json={
        "title": "Alice doc",
        "content": "Secret",
        "shared_with": [bob.id],
    })
    doc_id = res_doc.json()["id"]

    # Bob (shared recipient, not creator) attempts to share with Charlie -> 403 Forbidden
    client.cookies.set(COOKIE, "bob3_tok")
    res_forbidden = client.post(f"/api/docs/{doc_id}/share", json={"users": [charlie.id]})
    assert res_forbidden.status_code == 403



@pytest.mark.anyio
async def test_bot_created_document_scoped_to_asker_and_shared(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, DocumentRecord, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    # Setup project and bot
    proj = Project(id="p_meta", name="Meta Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_1", name="Bot One", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    # Setup users in project
    alice = make_user(db, "alice_bot", token="tok_alice_bot")
    bob = make_user(db, "bob_bot", token="tok_bob_bot")
    charlie = make_user(db, "charlie_bot", token="tok_charlie_bot")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=charlie.id, role="member"),
    ])
    db.commit()

    # Alice asks the bot to generate a document
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "spec_meta.md", "format": "md", "title": "Meta Spec", "content": "# Meta Content"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    actual_filename = m.group(1)

    # Verify DocumentRecord saved Alice as creator
    doc_rec = db.get(DocumentRecord, actual_filename)
    assert doc_rec is not None
    assert doc_rec.user_id == alice.id
    assert doc_rec.created_by == alice.id

    # Alice can see and download it
    client.cookies.set(COOKIE, "tok_alice_bot")
    res_a_proj_docs = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert res_a_proj_docs.status_code == 200
    assert any(d["filename"] == actual_filename for d in res_a_proj_docs.json())

    res_a_down = client.get(f"/api/documents/{actual_filename}")
    assert res_a_down.status_code == 200

    res_a_crud = client.get("/api/docs")
    assert res_a_crud.status_code == 200
    assert any(d["title"] == "Meta Spec" for d in res_a_crud.json())

    # Bob (member of same project) cannot see or download it
    client.cookies.set(COOKIE, "tok_bob_bot")
    res_b_proj_docs = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert res_b_proj_docs.status_code == 200
    assert not any(d["filename"] == actual_filename for d in res_b_proj_docs.json())

    res_b_down = client.get(f"/api/documents/{actual_filename}")
    assert res_b_down.status_code == 404

    res_b_crud = client.get("/api/docs")
    assert not any(d["title"] == "Meta Spec" for d in res_b_crud.json())

    # Alice shares it with Bob
    client.cookies.set(COOKIE, "tok_alice_bot")
    res_share = client.post(f"/api/documents/{actual_filename}/share", json={"users": [bob.id]})
    assert res_share.status_code == 200
    assert bob.id in res_share.json()["shared_with"]

    # Now Bob CAN see and download it
    client.cookies.set(COOKIE, "tok_bob_bot")
    res_b_proj_docs2 = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert any(d["filename"] == actual_filename for d in res_b_proj_docs2.json())

    res_b_down2 = client.get(f"/api/documents/{actual_filename}")
    assert res_b_down2.status_code == 200

    # Charlie still cannot see it
    client.cookies.set(COOKIE, "tok_charlie_bot")
    res_c_down = client.get(f"/api/documents/{actual_filename}")
    assert res_c_down.status_code == 404


@pytest.mark.anyio
async def test_bot_generated_doc_forwarded_to_person_b(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, DocumentRecord, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_fwd", name="Forward Test Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_fwd", name="Fwd Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_fwd", token="tok_alice_fwd")
    bob = make_user(db, "bob_fwd", token="tok_bob_fwd")
    charlie = make_user(db, "charlie_fwd", token="tok_charlie_fwd")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=charlie.id, role="member"),
    ])
    db.commit()

    # Alice asks bot to generate a doc
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "private_report.md", "format": "md", "title": "Alice Private Report", "content": "Confidential research"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Alice can see it
    client.cookies.set(COOKIE, "tok_alice_fwd")
    res_a_docs = client.get("/api/docs")
    assert any(d["title"] == "Alice Private Report" for d in res_a_docs.json())

    # Bob cannot see it
    client.cookies.set(COOKIE, "tok_bob_fwd")
    res_b_docs = client.get("/api/docs")
    assert not any(d["title"] == "Alice Private Report" for d in res_b_docs.json())
    assert client.get(f"/api/docs/{filename}").status_code == 404

    # Alice forwards the document to Bob via direct message
    client.cookies.set(COOKIE, "tok_alice_fwd")
    forward_msg = f"Forwarded from Alice:\n\nHere is the doc: /api/documents/{filename}"
    res_fwd = client.post(f"/api/direct/{bob.id}", headers={"x-project-id": proj.id}, json={"content": forward_msg})
    assert res_fwd.status_code == 201

    # Now Bob CAN see the document in /docs and GET /docs/{id}
    client.cookies.set(COOKIE, "tok_bob_fwd")
    res_b_docs2 = client.get("/api/docs")
    assert any(d["title"] == "Alice Private Report" for d in res_b_docs2.json())

    res_b_single = client.get(f"/api/docs/{filename}")
    assert res_b_single.status_code == 200
    assert res_b_single.json()["title"] == "Alice Private Report"

    # Charlie still cannot see it
    client.cookies.set(COOKIE, "tok_charlie_fwd")
    res_c_docs = client.get("/api/docs")
    assert not any(d["title"] == "Alice Private Report" for d in res_c_docs.json())
    assert client.get(f"/api/docs/{filename}").status_code == 404

def test_delete_doc_endpoint(db, client):
    alice = make_user(db, "alice_del", token="tok_alice_del")
    client.cookies.set(COOKIE, "tok_alice_del")

    # Alice creates a doc
    res = client.post("/api/docs", json={"title": "To Delete", "content": "Will be removed"})
    assert res.status_code == 201
    doc_id = res.json()["id"]

    # Doc exists
    assert client.get(f"/api/docs/{doc_id}").status_code == 200

    # Delete doc
    res_del = client.delete(f"/api/docs/{doc_id}")
    assert res_del.status_code == 204

    # Now it is 404
    assert client.get(f"/api/docs/{doc_id}").status_code == 404


@pytest.mark.anyio
async def test_delete_api_documents_endpoint(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_fdel", name="Delete Test Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_fdel", name="Del Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_file_del", token="tok_alice_fdel")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    client.cookies.set(COOKIE, "tok_alice_fdel")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "to_delete.md", "format": "md", "title": "File To Delete", "content": "Content to be deleted"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # File exists in GET /api/documents
    res_list = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert any(d["filename"] == filename for d in res_list.json())

    # Delete via DELETE /api/documents/{filename}
    res_del = client.delete(f"/api/documents/{filename}")
    assert res_del.status_code == 204

    # File is now gone from GET /api/documents
    res_list2 = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert not any(d["filename"] == filename for d in res_list2.json())

@pytest.mark.anyio
async def test_edit_markdown_document_endpoint(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_edit", name="Edit Test Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_edit", name="Edit Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_edit", token="tok_alice_edit")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    client.cookies.set(COOKIE, "tok_alice_edit")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "doc_to_edit.md", "format": "md", "title": "Original Title", "content": "# Initial Header\nInitial text"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Edit the markdown document via PUT /api/documents/{filename}
    updated_markdown = "# Updated Header\nThis document has been updated!"
    res_put = client.put(f"/api/documents/{filename}", content=updated_markdown.encode("utf-8"), headers={"Content-Type": "text/markdown; charset=utf-8"})
    assert res_put.status_code == 200

    # Fetch document content via GET /api/documents/{filename}
    res_get = client.get(f"/api/documents/{filename}")
    assert res_get.status_code == 200
    assert "This document has been updated!" in res_get.text

@pytest.mark.anyio
async def test_delete_unauthorized_user_cannot_delete_other_user_doc(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_unauth_del", name="Unauth Delete Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_unauth", name="Unauth Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_del_owner", token="tok_alice_del_owner")
    bob = make_user(db, "bob_del_intruder", token="tok_bob_del_intruder")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
    ])
    db.commit()

    # Alice creates a private doc
    client.cookies.set(COOKIE, "tok_alice_del_owner")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "alice_secret.md", "format": "md", "title": "Alice Secret Doc", "content": "Top secret"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Bob attempts to delete Alice's doc via DELETE /api/documents/{filename} -> 403 Forbidden
    client.cookies.set(COOKIE, "tok_bob_del_intruder")
    res_del_api = client.delete(f"/api/documents/{filename}")
    assert res_del_api.status_code == 403

    # Bob attempts to delete via /docs/{filename} -> 404 (hidden from Bob)
    res_del_doc = client.delete(f"/api/docs/{filename}")
    assert res_del_doc.status_code == 404


@pytest.mark.anyio
async def test_share_document_api_endpoint(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_share_api", name="Share API Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_share_api", name="Share Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_sharer", token="tok_alice_sharer")
    bob = make_user(db, "bob_receiver", token="tok_bob_receiver")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
    ])
    db.commit()

    # Alice creates a private doc
    client.cookies.set(COOKIE, "tok_alice_sharer")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "shared_resource.md", "format": "md", "title": "Shared Resource", "content": "Knowledge base"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Bob initially cannot see it
    client.cookies.set(COOKIE, "tok_bob_receiver")
    res_b_list = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert not any(d["filename"] == filename for d in res_b_list.json())

    # Alice shares it with Bob via POST /api/documents/{filename}/share
    client.cookies.set(COOKIE, "tok_alice_sharer")
    res_share = client.post(f"/api/documents/{filename}/share", json={"users": [bob.id]})
    assert res_share.status_code == 200
    assert bob.id in res_share.json()["shared_with"]

    # Now Bob can see it in GET /api/documents and download it
    client.cookies.set(COOKIE, "tok_bob_receiver")
    res_b_list2 = client.get("/api/documents", headers={"x-project-id": proj.id})
    assert any(d["filename"] == filename for d in res_b_list2.json())

    res_b_get = client.get(f"/api/documents/{filename}")
    assert res_b_get.status_code == 200
    assert "Knowledge base" in res_b_get.text


@pytest.mark.anyio
async def test_share_via_direct_message_markdown_link(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_dm_share", name="DM Share Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_dm_share", name="DM Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_dm", token="tok_alice_dm")
    bob = make_user(db, "bob_dm", token="tok_bob_dm")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
    ])
    db.commit()

    # Alice creates a private doc
    client.cookies.set(COOKIE, "tok_alice_dm")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "project_roadmap.md", "format": "md", "title": "Project Roadmap", "content": "Q4 Priorities"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Bob initially cannot see it
    client.cookies.set(COOKIE, "tok_bob_dm")
    assert client.get(f"/api/docs/{filename}").status_code == 404

    # Alice shares via direct message with Markdown link format [Title](url) without old forward prefix
    client.cookies.set(COOKIE, "tok_alice_dm")
    msg_content = f"Check this out:\n\n[Project Roadmap](/api/documents/{filename})"
    res_msg = client.post(f"/api/direct/{bob.id}", headers={"x-project-id": proj.id}, json={"content": msg_content})
    assert res_msg.status_code == 201

    # Now Bob has access and can see it in GET /docs/{id}
    client.cookies.set(COOKIE, "tok_bob_dm")
    res_b_doc = client.get(f"/api/docs/{filename}")
    assert res_b_doc.status_code == 200
    assert res_b_doc.json()["title"] == "Project Roadmap"


@pytest.mark.anyio
async def test_put_document_rejects_non_editable_extension(db, client, tmp_path, monkeypatch):
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_reject_ext", name="Reject Ext Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_reject", name="Reject Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_reject", token="tok_alice_reject")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    # Create a PDF document
    client.cookies.set(COOKIE, "tok_alice_reject")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "document.pdf", "format": "pdf", "title": "PDF Document", "content": "Fake PDF content"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    import re
    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Attempt to PUT/edit a PDF -> 400 Bad Request
    res_put = client.put(f"/api/documents/{filename}", content=b"New PDF binary data", headers={"Content-Type": "application/pdf"})
    assert res_put.status_code == 400
    assert "Only slideshows and markdown documents can be edited here." in res_put.json()["detail"]



@pytest.mark.anyio
async def test_unowned_document_visibility_scoped_to_project_members_never_guests(db, client, tmp_path, monkeypatch):
    """Unowned documents are visible to project members only, never guests or non-members (Finding 1)."""
    import tools.documents as documents
    from core.models import Agent, DocumentRecord, Project, ProjectMember

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_unowned", name="Unowned Test Project")
    db.add(proj)
    db.commit()

    alice = make_user(db, "alice_unowned", token="tok_alice_unowned")
    bob_non_member = make_user(db, "bob_outsider", token="tok_bob_outsider")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    # Create an unowned DocumentRecord (no user_id and no created_by)
    doc_path = tmp_path / "legacy_notes-20261028-000000.md"
    doc_path.write_text("# Legacy Notes\nShared project knowledge", encoding="utf-8")
    rec = DocumentRecord(
        filename=doc_path.name,
        agent_id=None,
        project_id=proj.id,
        user_id="",
        created_by="",
        shared_with=[],
        title="Legacy Notes",
        created_at=datetime.utcnow(),
    )
    db.add(rec)
    db.commit()

    # 1. Guest (unauthenticated) CANNOT see unowned document
    client.cookies.clear()
    res_guest_get = client.get(f"/api/docs/{doc_path.name}")
    assert res_guest_get.status_code == 404
    res_guest_list = client.get("/api/docs")
    assert not any(d["id"] == doc_path.name for d in res_guest_list.json())

    # 2. Project member CAN see unowned document in the project
    client.cookies.set(COOKIE, "tok_alice_unowned")
    res_member_get = client.get(f"/api/docs/{doc_path.name}")
    assert res_member_get.status_code == 200
    assert res_member_get.json()["title"] == "Legacy Notes"

    res_member_list = client.get("/api/docs")
    assert any(d["id"] == doc_path.name for d in res_member_list.json())

    # 3. Outsider (non-project member) CANNOT see unowned document
    client.cookies.set(COOKIE, "tok_bob_outsider")
    res_outsider_get = client.get(f"/api/docs/{doc_path.name}")
    assert res_outsider_get.status_code == 404


@pytest.mark.anyio
async def test_dm_auto_share_requires_sender_ownership(db, client, tmp_path, monkeypatch):
    """DM auto-share only grants access when the sender is the document owner (Finding 2)."""
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_dm_perm", name="DM Perm Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_dm_perm", name="DM Perm Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_dm_owner", token="tok_alice_dm_owner")
    bob = make_user(db, "bob_dm_reader", token="tok_bob_dm_reader")
    charlie = make_user(db, "charlie_dm_target", token="tok_charlie_dm_target")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=charlie.id, role="member"),
    ])
    db.commit()

    # Alice creates a document and shares it with Bob
    client.cookies.set(COOKIE, "tok_alice_dm_owner")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "confidential_strategy.md", "format": "md", "title": "Confidential Strategy", "content": "Strategy content"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Alice shares it with Bob via share endpoint
    res_share = client.post(f"/api/documents/{filename}/share", json={"users": [bob.id]})
    assert res_share.status_code == 200

    # Bob has read access, but is NOT the creator/owner
    client.cookies.set(COOKIE, "tok_bob_dm_reader")
    assert client.get(f"/api/docs/{filename}").status_code == 200

    # Charlie cannot see it yet
    client.cookies.set(COOKIE, "tok_charlie_dm_target")
    assert client.get(f"/api/docs/{filename}").status_code == 404

    # Bob attempts to forward/DM the document link to Charlie
    client.cookies.set(COOKIE, "tok_bob_dm_reader")
    res_fwd_bob = client.post(
        f"/api/direct/{charlie.id}",
        headers={"x-project-id": proj.id},
        json={"content": f"Hey Charlie, check this link: /api/documents/{filename}"},
    )
    assert res_fwd_bob.status_code == 201

    # Charlie still CANNOT see it because Bob is not the owner
    client.cookies.set(COOKIE, "tok_charlie_dm_target")
    assert client.get(f"/api/docs/{filename}").status_code == 404

    # Now Alice (the owner) forwards the document link to Charlie
    client.cookies.set(COOKIE, "tok_alice_dm_owner")
    res_fwd_alice = client.post(
        f"/api/direct/{charlie.id}",
        headers={"x-project-id": proj.id},
        json={"content": f"Hey Charlie, here is the doc: /api/documents/{filename}"},
    )
    assert res_fwd_alice.status_code == 201

    # Now Charlie CAN see it because the owner forwarded it
    client.cookies.set(COOKIE, "tok_charlie_dm_target")
    assert client.get(f"/api/docs/{filename}").status_code == 200


def test_db_row_string_shared_with_parses_properly(db, client):
    """DB row with string shared_with from legacy/migration is parsed correctly by _as_list and can_read (Finding 5)."""
    from tools.documents import _as_list, can_read
    from core.models import Doc as DocModel
    from sqlalchemy import text

    # String format from database migration
    raw_str = '["alice@example.com", "bob@example.com"]'
    parsed = _as_list(raw_str)
    assert parsed == ["alice@example.com", "bob@example.com"]

    comma_str = "alice@example.com, bob@example.com"
    assert _as_list(comma_str) == ["alice@example.com", "bob@example.com"]

    already_list = ["alice@example.com"]
    assert _as_list(already_list) == ["alice@example.com"]

    # Insert a real DB row with string shared_with to verify can_read and DB retrieval
    db.execute(text("INSERT INTO docs (id, title, content, tags, created_by, shared_with) VALUES ('str_sw_doc_1', 'Str SW Doc', 'content', '[]', 'owner_1', '[\"shared_user@example.com\"]')"))
    db.commit()
    doc = db.get(DocModel, "str_sw_doc_1")
    assert doc is not None
    assert _as_list(doc.shared_with) == ["shared_user@example.com"]
    assert can_read(doc, "other_user", identifiers={"shared_user@example.com"}) is True
    assert can_read(doc, "unshared_user", identifiers={"unshared_user@example.com"}) is False


@pytest.mark.anyio
async def test_markdown_put_requires_ownership_and_exact_id_match(db, client, tmp_path, monkeypatch):
    """PUT /api/documents/{filename} requires ownership and looks up Doc by id == filename only (Finding 6)."""
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Doc as DocModel, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_put_perm", name="PUT Perm Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_put_perm", name="PUT Perm Bot", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_put_owner", token="tok_alice_put_owner")
    bob = make_user(db, "bob_put_non_owner", token="tok_bob_put_non_owner")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
    ])
    db.commit()

    # Alice creates a private markdown document
    client.cookies.set(COOKIE, "tok_alice_put_owner")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        made = await run_tool(
            "create_document",
            {"filename": "contract.md", "format": "md", "title": "Contract Spec", "content": "Original contract"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    m = re.search(r"/api/documents/([^\s\)]+)", made)
    assert m is not None
    filename = m.group(1)

    # Bob creates an unrelated Doc that happens to have title == filename
    bob_doc = DocModel(
        id=str(uuid.uuid4()),
        title=filename,  # Same as Alice's filename
        content="Bob unrelated content",
        created_by=bob.id,
        user_id=bob.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(bob_doc)
    db.commit()

    # Bob (member of same project, but not owner of contract) attempts to PUT /api/documents/{filename}
    client.cookies.set(COOKIE, "tok_bob_put_non_owner")
    res_bob_put = client.put(
        f"/api/documents/{filename}",
        content="# Malicious Edit\nOverwritten text".encode("utf-8"),
        headers={"Content-Type": "text/markdown; charset=utf-8"},
    )
    # Must be 404 (hidden because not shared) or 403 (forbidden if checked)
    assert res_bob_put.status_code in (403, 404)

    # Verify Bob's unrelated Doc.content was NOT changed
    db.refresh(bob_doc)
    assert bob_doc.content == "Bob unrelated content"

    # Alice (owner) can edit successfully
    client.cookies.set(COOKIE, "tok_alice_put_owner")
    res_alice_put = client.put(
        f"/api/documents/{filename}",
        content="# Alice Legitimate Edit\nUpdated contract text".encode("utf-8"),
        headers={"Content-Type": "text/markdown; charset=utf-8"},
    )
    assert res_alice_put.status_code == 200

    # Bob's unrelated Doc.content is STILL unchanged
    db.refresh(bob_doc)
    assert bob_doc.content == "Bob unrelated content"


@pytest.mark.anyio
async def test_unowned_document_with_no_project_is_unreadable(db, client, tmp_path, monkeypatch):
    """Unowned document with no project_id is unreadable even by project members (Finding 2)."""
    import tools.documents as documents
    from core.models import DocumentRecord, Project, ProjectMember

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_any_member", name="Any Member Project")
    db.add(proj)
    db.commit()

    alice = make_user(db, "alice_member", token="tok_alice_member")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    # Create unowned document with NO project_id
    orphan_path = tmp_path / "orphan-20261028-000000.md"
    orphan_path.write_text("# Orphan\nNo project attached", encoding="utf-8")
    orphan_rec = DocumentRecord(
        filename=orphan_path.name,
        agent_id=None,
        project_id=None,
        user_id="",
        created_by="",
        shared_with=[],
        title="Orphan Document",
        created_at=datetime.utcnow(),
    )
    db.add(orphan_rec)
    db.commit()

    # Even though Alice is a project member of proj, orphan doc has no project so can_read must be False
    assert documents.can_read(orphan_rec, alice.id, project_ids={proj.id}, db=db) is False

    client.cookies.set(COOKIE, "tok_alice_member")
    res_get = client.get(f"/api/docs/{orphan_path.name}")
    assert res_get.status_code == 404


@pytest.mark.anyio
async def test_record_second_file_with_same_title(db, client, tmp_path, monkeypatch):
    """record() creates a Doc for a second file even if it has the same title (Finding 4)."""
    import tools.documents as documents
    from tools.dispatch import run_tool
    from core.models import Agent, Doc as DocModel, DocumentRecord, Project, ProjectMember
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_same_title", name="Same Title Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_same_title", name="Bot Same Title", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_same_title", token="tok_alice_same_title")
    db.add(ProjectMember(project_id=proj.id, user_id=alice.id, role="member"))
    db.commit()

    client.cookies.set(COOKIE, "tok_alice_same_title")
    u_token = set_usage_user(alice.id, proj.id)
    try:
        # File 1 with title "Status Report"
        res1 = await run_tool(
            "create_document",
            {"filename": "report_v1.md", "format": "md", "title": "Status Report", "content": "# Status Report\nVersion 1"},
            agent_id=bot.id,
        )
        # File 2 with same title "Status Report"
        res2 = await run_tool(
            "create_document",
            {"filename": "report_v2.md", "format": "md", "title": "Status Report", "content": "# Status Report\nVersion 2"},
            agent_id=bot.id,
        )
    finally:
        reset_usage_user(u_token)

    m1 = re.search(r"/api/documents/([^\s\)]+)", res1)
    m2 = re.search(r"/api/documents/([^\s\)]+)", res2)
    assert m1 is not None and m2 is not None
    f1 = m1.group(1)
    f2 = m2.group(1)
    assert f1 != f2

    # Both DocumentRecords must exist
    rec1 = db.get(DocumentRecord, f1)
    rec2 = db.get(DocumentRecord, f2)
    assert rec1 is not None and rec2 is not None
    assert rec1.title == "Status Report" and rec2.title == "Status Report"

    # Both Doc rows must exist with their respective filenames as id
    doc1 = db.get(DocModel, f1)
    doc2 = db.get(DocModel, f2)
    assert doc1 is not None, f"First doc {f1} missing"
    assert doc2 is not None, f"Second doc {f2} missing (was deduplicated incorrectly)"
    assert doc1.id == f1 and doc2.id == f2
    assert "Version 1" in doc1.content
    assert "Version 2" in doc2.content


@pytest.mark.anyio
async def test_long_filename_doc_insert_and_non_string_content(db, client, tmp_path, monkeypatch):
    """Doc.id handles long filenames up to 140 chars and non-string content safely (Finding 3, 11)."""
    import tools.documents as documents
    from core.models import Agent, Doc as DocModel, DocumentRecord, Project
    from core.usage import set_usage_user, reset_usage_user

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_long_name", name="Long Name Project")
    db.add(proj)
    db.commit()
    bot = Agent(id="bot_long_name", name="Bot Long Name", system_prompt="", project_id=proj.id)
    db.add(bot)
    db.commit()

    alice = make_user(db, "alice_long_name", token="tok_alice_long_name")

    # Filename length: 110 chars (> 36)
    long_filename = "detailed_system_architecture_report_with_extensive_descriptive_analysis_and_tables_20261028-123456.md"
    assert 36 < len(long_filename) <= 140

    long_path = tmp_path / long_filename
    long_path.write_text("# Long Doc Content", encoding="utf-8")

    u_token = set_usage_user(alice.id, proj.id)
    try:
        # Pass non-string content (dict) to verify coercion in record()
        documents.record(
            agent_id=bot.id,
            result=f"Here is your file: [Architecture](/api/documents/{long_filename})",
            title="Architecture",
            content={"type": "spreadsheet", "rows": [1, 2, 3]},
        )
    finally:
        reset_usage_user(u_token)

    rec = db.get(DocumentRecord, long_filename)
    assert rec is not None
    assert rec.filename == long_filename

    doc = db.get(DocModel, long_filename)
    assert doc is not None
    assert doc.id == long_filename
    assert len(doc.id) > 36
    assert isinstance(doc.content, str)


@pytest.mark.anyio
async def test_share_endpoint_with_same_title_doc_owned_by_another_user(db, client, tmp_path, monkeypatch):
    """Sharing a document does not touch an unrelated document with the same title owned by another user (Finding 4)."""
    import tools.documents as documents
    from core.models import Agent, Doc as DocModel, DocumentRecord, Project, ProjectMember

    monkeypatch.setattr(documents, "DOC_DIR", tmp_path)
    monkeypatch.setattr(documents, "_r2", lambda: None)

    proj = Project(id="p_share_same_title", name="Share Same Title Project")
    db.add(proj)
    db.commit()

    alice = make_user(db, "alice_sharer", token="tok_alice_sharer")
    bob = make_user(db, "bob_same_title_owner", token="tok_bob_same_title_owner")
    db.add_all([
        ProjectMember(project_id=proj.id, user_id=alice.id, role="member"),
        ProjectMember(project_id=proj.id, user_id=bob.id, role="member"),
    ])
    db.commit()

    # Alice's doc
    alice_doc_id = "alice_doc_123"
    db.add(DocModel(
        id=alice_doc_id,
        title="Project Roadmap",
        content="Alice roadmap",
        created_by=alice.id,
        user_id=alice.id,
        shared_with=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    ))
    db.add(DocumentRecord(
        filename=alice_doc_id,
        agent_id=None,
        project_id=proj.id,
        user_id=alice.id,
        created_by=alice.id,
        shared_with=[],
        title="Project Roadmap",
        created_at=datetime.utcnow(),
    ))

    # Bob's doc with the exact same title
    bob_doc_id = "bob_doc_456"
    db.add(DocModel(
        id=bob_doc_id,
        title="Project Roadmap",
        content="Bob roadmap",
        created_by=bob.id,
        user_id=bob.id,
        shared_with=[],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    ))
    db.add(DocumentRecord(
        filename=bob_doc_id,
        agent_id=None,
        project_id=proj.id,
        user_id=bob.id,
        created_by=bob.id,
        shared_with=[],
        title="Project Roadmap",
        created_at=datetime.utcnow(),
    ))
    db.commit()

    # 1. Alice shares her doc with Charlie
    client.cookies.set(COOKIE, "tok_alice_sharer")
    res_share = client.post(
        f"/api/docs/{alice_doc_id}/share",
        json={"users": ["charlie@example.com"]},
    )
    assert res_share.status_code == 200
    assert "charlie@example.com" in res_share.json()["shared_with"]

    # Verify Alice's doc was updated
    alice_doc = db.get(DocModel, alice_doc_id)
    assert "charlie@example.com" in alice_doc.shared_with

    # Verify Bob's doc was NOT updated and remains unshared
    bob_doc = db.get(DocModel, bob_doc_id)
    assert bob_doc.shared_with == []
    bob_rec = db.get(DocumentRecord, bob_doc_id)
    assert bob_rec.shared_with == []

    # 2. Bob tries to share Alice's doc using Alice's doc ID -> Forbidden
    client.cookies.set(COOKIE, "tok_bob_same_title_owner")
    res_forbidden = client.post(
        f"/api/docs/{alice_doc_id}/share",
        json={"users": ["dave@example.com"]},
    )
    assert res_forbidden.status_code in (403, 404)

    # Verify Alice's doc still only has Charlie
    db.refresh(alice_doc)
    assert "dave@example.com" not in alice_doc.shared_with

