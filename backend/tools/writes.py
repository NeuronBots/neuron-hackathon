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
    }.get(name) or _words(name)
    if name.startswith("gmail_") and args.get("to"):
        head += f" to {args['to']}"
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
