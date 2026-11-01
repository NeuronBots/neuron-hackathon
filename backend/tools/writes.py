"""Writes a bot makes through a connected account (Jira, GitHub, Gmail): which calls they are, what they will do, and what to tell the other person once done."""
import json
import re

from core.audience import scrub

_READ = re.compile(r"^(?:github_|gmail_)?(?:get|list|search|read|fetch|lookup|find|query|describe|whoami|executeRead|atlassianUserInfo)", re.IGNORECASE)
_KEY = re.compile(r'"key"\s*:\s*"([A-Z][A-Z0-9_]*-\d+)"')


def is_write(name: str) -> bool:
    """Anything that is not plainly a read counts as a write, so a tool we have not seen holds for approval instead of running."""
    return not _READ.match(name)


def _words(name: str) -> str:
    name = re.sub(r"^(?:github_|gmail_)", "", name)
    return re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", name).strip().capitalize()


def _text(value, limit: int = 120) -> str:
    return scrub(str(value or ""), limit)


def describe(name: str, args: dict) -> str:
    """What the asker is approving: a headline, then the whole call, untruncated. The card is the only gate for a write, so nothing the call
    will do may sit beyond what they can read."""
    key = args.get("issueIdOrKey") or args.get("issueKey") or ""
    head = {
        "createJiraIssue": f"Create {args.get('issueTypeName') or 'a story'} in Jira project {args.get('projectKey') or ''}: “{args.get('summary') or ''}”",
        "editJiraIssue": f"Edit {key} in Jira",
        "transitionJiraIssue": f"Move {key} to a new status in Jira",
        "addCommentToJiraIssue": f"Comment on {key} in Jira",
        "addOrEditJiraIssueComment": f"Comment on {key} in Jira",
        "start_coding_session": "Start a live coding session: the built app runs in a sandbox beside the chat and Muse changes it as you ask",
        "push_pr_fix": "Push the fixes for a pull request's review findings to its branch on GitHub",
        "publish_build": "Open a pull request with the project's first build on its repository",
        "publish_site": "Publish the project's built website on Neuron: it becomes public on the internet" + (f" (build {args['archive']})" if args.get("archive") else ""),
        "update_site": f"Change {args.get('path') or 'a file'} on the public website",
        "add_routine": f"Add the routine “{args.get('title') or ''}”" + (f" for {args['bot']}" if args.get("bot") else "") + ": it runs by itself every time an order comes in from the website",
        "update_routine": f"Change the routine “{args.get('title') or ''}”" + (f" of {args['bot']}" if args.get("bot") else "") + ": this is what runs by itself on every order from the website",
        "add_payments": "Turn on online payments for the project's site: Muse adds checkout to its order form, customers pay on a Razorpay page, and Neuron takes 5% of each payment (3% Neuron, about 2% gateway). Money is held 7 days for refunds",
        "build_project": "Have Muse build the project's first version from its plan, in a sandbox" + (f" ({args['plan']})" if args.get("plan") else "") + ". It writes and runs the code, saves it in Docs and offers a pull request",
        "build_wiki": "Build or update this project's GitHub wiki" + (f", focusing on: {args['focus']}" if args.get("focus") else "") + ". Pages people wrote are left untouched",
    }.get(name) or _words(name)
    if name.startswith("gmail_") and args.get("to"):
        to = args["to"]
        head += " to " + (", ".join(str(t) for t in to) if isinstance(to, (list, tuple)) else str(to))
    body = json.dumps(args, ensure_ascii=False, indent=2)
    fence = "~~~"
    while fence in body:
        fence += "~"
    return f"{head.strip()}\n\nThe whole call:\n{fence}json\n{name} {body}\n{fence}"


def done_note(name: str, args: dict, result: str) -> str:
    """One sentence for the other person: that it was done, not what the account held."""
    given = str(args.get("issueIdOrKey") or args.get("issueKey") or "")
    key = given if re.fullmatch(r"[A-Z][A-Z0-9_]*-\d+", given) else ""
    title = _text(args.get("summary") or args.get("title"))
    made = (_KEY.search(result or "") or [None, ""])[1]
    if name == "createJiraIssue":
        return f"{_text(args.get('issueTypeName') or 'Story', 30)} “{title}” has been created in Jira{f' ({made})' if made else ''}."
    jira = {
        "editJiraIssue": f"{key or 'An issue'} has been updated in Jira.",
        "transitionJiraIssue": f"{key or 'An issue'} has been moved to a new status in Jira.",
        "addCommentToJiraIssue": f"A comment has been added to {key or 'an issue'} in Jira.",
        "addOrEditJiraIssueComment": f"A comment has been added to {key or 'an issue'} in Jira.",
        "addWorklogToJiraIssue": f"Work has been logged on {key or 'an issue'} in Jira.",
        "manageJiraSprint": "A sprint has been updated in Jira.",
        "planSprint": "A sprint has been planned in Jira.",
        "attachDocumentToJiraIssue": f"A document has been attached to {key or 'an issue'} in Jira.",
        "build_wiki": "The GitHub wiki is being built; it will be published in a few minutes.",
        "publish_build": "A pull request with the first build has been opened.",
        "publish_site": "The website has been published" + (f": {m.group(0)}" if (m := re.search(r"https?://\S+", result or "")) else "."),
        "update_site": "The public website has been changed.",
        "add_routine": f"The routine “{_text(args.get('title'))}” has been added: it runs on every new order.",
        "update_routine": f"The routine “{_text(args.get('title'))}” has been changed.",
        "add_payments": "Payments are on: Muse is adding checkout to the site.",
        "start_coding_session": "The coding session is starting; the app appears beside the chat.",
        "push_pr_fix": "The fixes have been pushed to the pull request's branch.",
        "build_project": "Muse has started building the first version; progress shows in the chat.",
    }
    if name in jira:
        return jira[name]
    if name == "github_create_issue":
        return f"GitHub issue “{title}” has been opened."
    if name == "github_create_pull_request":
        return f"Pull request “{title}” has been opened on GitHub."
    if name.startswith("gmail_"):
        return "An email has been sent." if "send" in name else "An email draft has been created."
    return f"{_words(name)}: done."
