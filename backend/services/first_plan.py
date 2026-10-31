"""A new project's first piece of work: the team plans the first build.

Right after a project is created its Master is started on a board task. It reads what there is (the goal, the repository), asks its teammates for
their specialist input, takes the few decisions that matter to the person through Decisions pending (with a recommendation each), and then writes
ONE detailed plan as a document that the first code can be built from. The plan is a document, not code: nothing is built until the person has read it.

It uses only what bots already have (the board, request_decision, ask_teammate, create_document); anything it does outside the app (Jira, GitHub
writes) still waits for approval like any other outward action."""
from __future__ import annotations

from sqlalchemy.orm import Session

from core.models import Agent, Project, Task

DOC_PREFIX = "First build plan"  # the plan document's title starts with this; a button under it starts the build (services/first_build.py)
TITLE = "Draft the first-build plan"  # the board task's title; decisions about the plan use it too, so they stay attached to it


def brief(name: str, goal: str, repo: str, summary: str, team: list[tuple[str, str, str]], person: str) -> str:
    """What the Master is told when the project is created."""
    mates = "\n".join(f"- {n}" + (f" ({', '.join(x for x in (label, field) if x)})" if label or field else "") for n, label, field in team) or "- (no teammates yet: do their parts yourself)"
    return (
        f"A new project was just created by {person or 'the owner'}. You lead it. Your first job, now and on your own, is to get the team to ONE detailed plan for the "
        "FIRST BUILD of this project, meaning the first code that will be written, and to guide the person through the few decisions that really change that plan.\n\n"
        f"## The project\nName: {name}\nGoal: {goal or '(not given: ask for it as your first decision)'}\n"
        + (f"Repository: {repo}\n" if repo else "Repository: none linked yet\n")
        + (f"What is in the repository: {summary}\n" if summary else "")
        + f"\n## Your team\n{mates}\n\n"
        "## How to work\n"
        f"1. Put the work on the board first: add_board_task with the title \"{TITLE}\" (it may already be there; then use it). Use that exact title whenever you "
        "call request_decision or complete_board_task for this plan.\n"
        "2. Understand before you plan. Read the goal"
        + (", then look at the repository with your GitHub tools (README, the folder layout, the manifests) so the plan fits what exists; do not guess what is there" if repo else "")
        + ". Check the project's documents with read_document if someone already wrote something.\n"
        "3. Research, with your team. Before you plan, find out what is already known: ask each teammate with ask_teammate to research their own field ON THE WEB: they must call web_search, then web_fetch "
        "on the best pages (a research document without at least two source links is refused) and save what they found as a short document titled \"Research: <topic>\" with the source links (create_document): what comparable products do, "
        "proven ways to build this, the risks, any standards or rules that apply, what it costs to run. Ask a designer for the experience (screens, flow, look) and a QA lead for the test "
        "cases that would prove the first build works, each as a document. Research the biggest open question yourself. Read what they save (read_document), let it decide the plan, "
        "and cite it. Their documents go to Muse with the plan when it builds, so ask for what Muse will need, and ask only the teammates whose field matters.\n"
        "4. Take the decisions that matter with the person. List what would change the plan: what the first build must include and leave out, the main technology "
        "choices, the data it keeps, outside services, how it will be hosted and who signs in. Settle the small things yourself and say so in the plan as assumptions. "
        f"For each decision that only the person can make, call request_decision with the title \"{TITLE}\", one clear question, your recommendation, and the options "
        "(two to four, best first) in the options field: they appear as buttons in the chat, so the person can answer with one tap. Fold related questions into one decision, put the most important first, and ask at most five in all "
        "over at most three rounds. When they answer, take it as final and continue from there.\n"
        "5. Write the plan. When the decisions are settled, call create_document ONCE, as md, titled \"" + DOC_PREFIX + ": " + name + "\". Give it enough detail that a developer "
        "or a coding assistant can build the first version from it without asking anything: the goal and what \"done\" means for the first build; what is in and out of scope; the "
        "decisions made and why; the architecture and technology; the data model; the work broken into ordered steps, each with what to build, how to tell it is finished and "
        "who on the team is involved; how the interface adapts to a phone, a tablet and a desktop (what changes at each width, mobile first); how it will be tested; risks and open questions; and the assumptions you made. Write the plan itself, not a description of one.\n"
        f"6. Finish. Call complete_board_task with \"{TITLE}\", give the person the link to the document exactly as the tool returns it, say in three lines what is in it, "
        "and ask them to read it and tell you what to change. A button under the plan starts Muse building it when they are happy, and if they ask you to build it, call build_project: tell them both. Do not build by asking teammates or adding tasks.\n\n"
        "## Rules\n"
        "- One document, not one per teammate. You assemble it.\n"
        "- Do not write or push code, and do not create Jira issues or anything outside the app: this is a plan. Anything that leaves the app needs the person's approval.\n"
        "- Start now. Begin with one short sentence to the person saying what you are doing; the project's chat is where they will see you work.\n"
    )


def queue(db: Session, project: Project, master: Agent, team: list[tuple[str, str, str]], person: str) -> str:
    """Put the planning on the Master's board, so it is visible and is picked up again if the server restarts before it begins. Returns the brief."""
    db.add(Task(agent_id=master.id, title=TITLE, status="working"))
    return brief(project.name, project.goal or "", project.repo or "", project.summary or "", team, person)


async def start(master_id: str, project_id: str, user_id: str, text: str) -> None:
    """Start the Master on the brief, in the background (it runs for as long as the planning takes, and the person is told in the project's chat)."""
    from routes.chat import _start_kickoff

    _start_kickoff(master_id, "Neuron", project_id, text, user_id)
