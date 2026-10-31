"""Creating a repository from the new-project steps: always as the signed-in person, only where they belong, and with errors they can act on."""
import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException

from routes import projects
from services import project_setup

_REAL = httpx.AsyncClient  # before any patch, so a second patch wraps this, not the first


def run(coro):
    return asyncio.run(coro)


class FakeGitHub:
    """Answers the three calls creating a repository makes, and remembers what it was asked."""

    def __init__(self, monkeypatch, login="tanay", orgs=("acme",), orgs_status=200, create_status=201, me_status=200, repos=(), repos_status=200, taken=()):
        self.sent, self.login, self.orgs, self.orgs_status, self.create_status, self.me_status = [], login, orgs, orgs_status, create_status, me_status
        self.repos, self.repos_status = list(repos), repos_status
        self.taken = set(taken)  # owner/name that already exist
        project_setup._own_repo_cache.clear()
        monkeypatch.setattr(project_setup.httpx, "AsyncClient", lambda **kw: _REAL(transport=httpx.MockTransport(self.handle), **kw))

    def handle(self, request):
        path, body = request.url.path, (json.loads(request.content) if request.content else None)
        self.sent.append((request.method, path, body, request.headers.get("authorization")))
        if path == "/user":
            return httpx.Response(self.me_status, json={"login": self.login})
        if path.startswith("/repos/") and request.method == "GET":
            return httpx.Response(200 if path[len("/repos/"):] in self.taken else 404, json={})
        if path == "/user/repos" and request.method == "GET":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(self.repos_status, json=self.repos[(page - 1) * 100: page * 100])
        if path == "/user/orgs":
            return httpx.Response(self.orgs_status, json=[{"login": o} for o in self.orgs])
        if request.method == "POST":
            owner = path.split("/")[2] if path.startswith("/orgs/") else self.login
            return httpx.Response(self.create_status, json={"full_name": f"{owner}/{body['name']}", "description": body["description"], "html_url": f"https://github.com/{owner}/{body['name']}", "private": body["private"]})
        return httpx.Response(404)

    def posts(self):
        return [s for s in self.sent if s[0] == "POST"]


def test_the_owners_are_the_person_first_then_their_organisations(monkeypatch):
    FakeGitHub(monkeypatch, orgs=("acme", "zeta"))
    assert run(project_setup.repo_owners("tok")) == [{"login": "tanay", "kind": "user"}, {"login": "acme", "kind": "org"}, {"login": "zeta", "kind": "org"}]


def test_an_organisation_policy_that_hides_the_list_still_leaves_their_own_account(monkeypatch):
    FakeGitHub(monkeypatch, orgs_status=403)
    assert run(project_setup.repo_owners("tok")) == [{"login": "tanay", "kind": "user"}]


def test_a_repository_is_created_on_their_own_account_by_default_as_them(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    made = run(project_setup.create_repo("tok_abc", "my-app", description="  A thing.  "))
    ((method, path, body, auth),) = gh.posts()
    assert (method, path) == ("POST", "/user/repos") and auth == "Bearer tok_abc"
    assert body == {"name": "my-app", "description": "A thing.", "private": True, "auto_init": True}  # private unless asked, with a first commit to read
    assert made == {"full_name": "tanay/my-app", "description": "A thing.", "language": "", "html_url": "https://github.com/tanay/my-app", "private": True}


def test_a_repository_can_be_created_in_an_organisation_they_belong_to_and_made_public(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    made = run(project_setup.create_repo("tok", "svc", owner="ACME", private=False))
    ((_, path, body, _),) = gh.posts()
    assert path == "/orgs/acme/repos" and body["private"] is False and made["full_name"] == "acme/svc"


def test_somewhere_they_do_not_belong_is_refused_before_anything_is_created(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    with pytest.raises(ValueError, match="your own account or in an organisation you belong to"):
        run(project_setup.create_repo("tok", "svc", owner="someone-elses-org"))
    assert gh.posts() == []


@pytest.mark.parametrize("name", ["", "has space", "a/b", "..", ".", "x.git", "ünï", "a" * 101, "semi;colon"])
def test_a_bad_name_is_refused_without_calling_github(monkeypatch, name):
    gh = FakeGitHub(monkeypatch)
    with pytest.raises(ValueError, match="letters, numbers"):
        run(project_setup.create_repo("tok", name))
    assert gh.sent == []


def test_github_refusals_become_messages_a_person_can_act_on(monkeypatch):
    FakeGitHub(monkeypatch, create_status=422)
    with pytest.raises(ValueError, match="tanay/my-app already exists"):
        run(project_setup.create_repo("tok", "my-app"))
    FakeGitHub(monkeypatch, create_status=403)
    with pytest.raises(ValueError, match="would not let you create a repository there"):
        run(project_setup.create_repo("tok", "my-app"))
    FakeGitHub(monkeypatch, me_status=401)
    with pytest.raises(ValueError, match="Reconnect GitHub"):
        run(project_setup.create_repo("tok", "my-app"))
    FakeGitHub(monkeypatch, create_status=500)
    with pytest.raises(RuntimeError, match="500"):
        run(project_setup.create_repo("tok", "my-app"))


def test_the_route_uses_the_persons_own_connection_and_never_a_shared_token(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    seen = []

    async def token(provider, user_id=None):
        seen.append((provider, user_id))
        return "tok_own"
    monkeypatch.setattr("integrations.connections.access_token", token)
    user = type("U", (), {"id": "u1"})()

    owners = run(projects.new_repo_owners(user=user))
    made = run(projects.new_repo(projects.NewRepoBody(name="proj", owner="acme"), user=user))

    assert seen == [("github", "u1"), ("github", "u1")] and owners["owners"][0] == {"login": "tanay", "kind": "user"}
    assert made["full_name"] == "acme/proj" and {s[3] for s in gh.sent} == {"Bearer tok_own"}
    assert projects.NewRepoBody(name="x").private is True


def test_without_a_github_connection_nothing_is_created(monkeypatch):
    gh = FakeGitHub(monkeypatch)

    async def none(provider, user_id=None):
        return None
    monkeypatch.setattr("integrations.connections.access_token", none)
    user = type("U", (), {"id": "u1"})()
    for call in (lambda: projects.new_repo(projects.NewRepoBody(name="proj"), user=user), lambda: projects.new_repo_owners(user=user)):
        with pytest.raises(HTTPException) as err:
            run(call())
        assert err.value.status_code == 400 and "Connect your GitHub" in err.value.detail
    assert gh.sent == []


def test_route_errors_map_to_400_for_the_person_and_503_for_github(monkeypatch):
    async def token(provider, user_id=None):
        return "t"
    monkeypatch.setattr("integrations.connections.access_token", token)
    user = type("U", (), {"id": "u1"})()
    FakeGitHub(monkeypatch, create_status=422)
    with pytest.raises(HTTPException) as err:
        run(projects.new_repo(projects.NewRepoBody(name="dup"), user=user))
    assert err.value.status_code == 400
    FakeGitHub(monkeypatch, create_status=500)
    with pytest.raises(HTTPException) as err:
        run(projects.new_repo(projects.NewRepoBody(name="boom"), user=user))
    assert err.value.status_code == 503


# ── Searching: only what is in the person's own account ──────────────

def repo(full, desc="", lang="Python"):
    return {"full_name": full, "description": desc, "language": lang, "html_url": f"https://github.com/{full}"}


MINE = [repo("tanay/neuron-app", "The Neuron app"), repo("acme/billing", "Invoices for neuron customers"), repo("tanay/dotfiles"), repo("acme/neuron-docs", "Docs"),
        repo("tanay/Lumen-Health", "A health thing")]


def search(monkeypatch, query, repos=MINE, user="u1", **kw):
    gh = FakeGitHub(monkeypatch, repos=repos, **kw)
    return gh, [r["full_name"] for r in run(project_setup.search_repos("tok_own", user, query))]


def test_search_looks_only_at_the_persons_own_repositories_never_at_github_at_large(monkeypatch):
    gh, found = search(monkeypatch, "neuron")
    assert found == ["tanay/neuron-app", "acme/neuron-docs", "acme/billing"]  # names first, then a match only in the description; each in the order GitHub listed them
    paths = [p for _, p, _, _ in gh.sent]
    assert paths == ["/user/repos"] and not any(p.startswith("/search") for p in paths)
    ((_, _, _, auth),) = gh.sent
    assert auth == "Bearer tok_own"


def test_every_word_must_match_and_case_does_not_matter(monkeypatch):
    assert search(monkeypatch, "LUMEN health")[1] == ["tanay/Lumen-Health"]
    assert search(monkeypatch, "neuron invoices")[1] == ["acme/billing"]
    assert search(monkeypatch, "kubernetes")[1] == []


def test_a_repository_that_is_not_in_their_account_is_not_found_even_by_its_exact_name(monkeypatch):
    gh, found = search(monkeypatch, "torvalds/linux")
    assert found == [] and [p for _, p, _, _ in gh.sent] == ["/user/repos"]  # no lookup of the repository itself either
    assert search(monkeypatch, "https://github.com/torvalds/linux")[1] == []


def test_an_exact_slug_or_url_finds_that_one_repository_when_it_is_theirs(monkeypatch):
    assert search(monkeypatch, "ACME/billing")[1] == ["acme/billing"]
    assert search(monkeypatch, "https://github.com/acme/billing.git")[1] == ["acme/billing"]


def test_at_most_eight_come_back(monkeypatch):
    many = [repo(f"tanay/app-{i}") for i in range(30)]
    assert len(search(monkeypatch, "app", repos=many)[1]) == 8


def test_a_long_list_is_read_page_by_page_and_stops_at_the_last(monkeypatch):
    many = [repo(f"tanay/r{i}") for i in range(150)]
    gh, found = search(monkeypatch, "r149", repos=many)
    assert found == ["tanay/r149"] and [p for _, p, _, _ in gh.sent] == ["/user/repos", "/user/repos"]


def test_the_list_is_remembered_briefly_per_person_and_a_new_repository_joins_it(monkeypatch):
    gh = FakeGitHub(monkeypatch, repos=MINE)
    run(project_setup.search_repos("t", "u1", "neuron"))
    run(project_setup.search_repos("t", "u1", "billing"))
    assert len(gh.sent) == 1  # typing a search does not re-read their repositories
    run(project_setup.search_repos("t", "u2", "neuron"))
    assert len(gh.sent) == 2  # another person has a list of their own
    run(project_setup.create_repo("t", "fresh"))
    run(project_setup.search_repos("t", "u1", "neuron"))
    assert len([s for s in gh.sent if s[:2] == ("GET", "/user/repos")]) == 3  # created a repository: read again


def test_search_errors_say_what_to_do(monkeypatch):
    with pytest.raises(ValueError, match="Reconnect GitHub"):
        search(monkeypatch, "x", repos_status=401)
    with pytest.raises(RuntimeError, match="500"):
        search(monkeypatch, "x", repos_status=500)
    with pytest.raises(ValueError, match="Enter a repository"):
        search(monkeypatch, "   ")


def test_the_search_route_uses_the_persons_own_connection_and_needs_one(monkeypatch):
    gh = FakeGitHub(monkeypatch, repos=MINE)
    seen = []

    async def token(provider, user_id=None):
        seen.append((provider, user_id))
        return "tok_own" if user_id == "u1" else None
    monkeypatch.setattr("integrations.connections.access_token", token)
    out = run(projects.find_repos(projects.RepoQuery(query="billing"), user=type("U", (), {"id": "u1"})()))
    assert [r["full_name"] for r in out["repos"]] == ["acme/billing"] and seen == [("github", "u1")]
    with pytest.raises(HTTPException) as err:
        run(projects.find_repos(projects.RepoQuery(query="billing"), user=type("U", (), {"id": "nobody"})()))
    assert err.value.status_code == 400 and "Connect your GitHub" in err.value.detail
    assert len(gh.sent) == 1  # the second call never reached GitHub


# ── the dry run, and creating only with the project ──────────────────

def test_a_dry_run_says_what_would_be_made_and_creates_nothing(monkeypatch):
    gh = FakeGitHub(monkeypatch)
    out = run(project_setup.check_new_repo("tok", "lumen", "", False, "A health app"))
    assert out == {"full_name": "tanay/lumen", "private": False, "description": "A health app"} and not gh.posts()
    assert ("GET", "/repos/tanay/lumen") in [(m, p) for m, p, _, _ in gh.sent]


def test_a_dry_run_refuses_a_taken_name_a_bad_name_and_a_place_they_do_not_belong_to(monkeypatch):
    gh = FakeGitHub(monkeypatch, taken=("tanay/lumen",))
    with pytest.raises(ValueError, match="already exists"):
        run(project_setup.check_new_repo("tok", "lumen"))
    with pytest.raises(ValueError, match="letters, numbers"):
        run(project_setup.check_new_repo("tok", "bad name!"))
    with pytest.raises(ValueError, match="own account or in an organisation"):
        run(project_setup.check_new_repo("tok", "lumen", "someone-else"))
    assert not gh.posts()


def test_the_dry_run_endpoint_maps_refusals_to_messages(monkeypatch, db):
    FakeGitHub(monkeypatch, taken=("tanay/lumen",))
    async def token(user):
        return "tok"
    monkeypatch.setattr(projects, "_own_github_token", token)
    assert run(projects.check_repo(projects.NewRepoBody(name="fresh"), user=None))["full_name"] == "tanay/fresh"
    with pytest.raises(HTTPException) as err:
        run(projects.check_repo(projects.NewRepoBody(name="lumen"), user=None))
    assert err.value.status_code == 400 and "already exists" in err.value.detail


def _project_world(db, monkeypatch, **gh):
    from core.access import hash_password
    from core.models import User
    user = User(id="user_n", email="n@example.com", name="Nia", password_hash=hash_password("testpass123"), plan="free", is_admin=True)
    db.add(user)
    db.commit()
    fake = FakeGitHub(monkeypatch, **gh)

    async def token(u):
        return "tok"
    monkeypatch.setattr(projects, "_own_github_token", token)
    return user, fake


def test_the_repository_is_made_when_the_project_is_and_linked_to_it(db, monkeypatch):
    from fastapi import BackgroundTasks
    from core.models import Agent, Project
    user, gh = _project_world(db, monkeypatch)
    body = projects.ProjectBody(name="Lumen", goal="A health app", new_repo=projects.NewRepoBody(name="lumen", description="A health app"), start_plan=False,
                                agents=[projects.SuggestedAgent(name="Ada", label="QA", field="qa", system_prompt="Ada tests.")], coding_bot="Ada")
    out = run(projects.create_project(body, BackgroundTasks(), user=user, db=db))
    assert out["repo"] == "tanay/lumen" and len(gh.posts()) == 1 and gh.posts()[0][2]["name"] == "lumen"
    project = db.get(Project, out["id"])
    assert project.repo == "tanay/lumen" and project.coding_bot_id  # the coding bot can be assigned, since there is now a repository
    master = db.query(Agent).filter(Agent.project_id == project.id, Agent.is_master == "true").one()
    assert "Repository: tanay/lumen." in master.system_prompt  # and the Master knows about it from its first message


def test_when_github_refuses_no_project_is_left_behind(db, monkeypatch):
    from fastapi import BackgroundTasks
    from core.models import Agent, Project, ProjectMember
    user, gh = _project_world(db, monkeypatch, create_status=403)
    body = projects.ProjectBody(name="Lumen", new_repo=projects.NewRepoBody(name="lumen"), start_plan=False,
                                agents=[projects.SuggestedAgent(name="Ada", label="QA", field="qa", system_prompt="x")])
    with pytest.raises(HTTPException) as err:
        run(projects.create_project(body, BackgroundTasks(), user=user, db=db))
    assert err.value.status_code == 400 and "would not let you" in err.value.detail
    assert db.query(Project).count() == 0 and db.query(ProjectMember).count() == 0 and db.query(Agent).count() == 0


def test_a_taken_name_is_caught_before_any_project_is_made_and_both_kinds_of_repository_are_refused(db, monkeypatch):
    from fastapi import BackgroundTasks
    from core.models import Project
    user, gh = _project_world(db, monkeypatch, taken=("tanay/lumen",))
    with pytest.raises(HTTPException) as err:
        run(projects.create_project(projects.ProjectBody(name="Lumen", new_repo=projects.NewRepoBody(name="lumen"), start_plan=False), BackgroundTasks(), user=user, db=db))
    assert "already exists" in err.value.detail and db.query(Project).count() == 0 and not gh.posts()
    with pytest.raises(HTTPException) as err:
        run(projects.create_project(projects.ProjectBody(name="Lumen", repo="acme/app", new_repo=projects.NewRepoBody(name="x"), start_plan=False), BackgroundTasks(), user=user, db=db))
    assert err.value.status_code == 400 and "not both" in err.value.detail
