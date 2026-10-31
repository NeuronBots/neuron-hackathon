"""What Muse is told when it builds: the skills it must use, the rules it works under, and the instructions."""
import pytest

from services import build_prompt as bp


def test_the_three_skills_are_in_the_workspace_with_their_licenses():
    files = bp.skill_files()
    for name in ("ponytail", "frontend-design", "single-responsibility"):
        assert f".agents/skills/{name}/SKILL.md" in files
        assert files[f".agents/skills/{name}/SKILL.md"].startswith("---\nname: " + name)  # a skill Muse will recognise by name
    assert ".agents/skills/ponytail/LICENSE" in files and ".agents/skills/frontend-design/LICENSE.txt" in files
    assert "Single responsibility" in files[".agents/skills/single-responsibility/SKILL.md"]


def test_every_instruction_the_build_depends_on_is_there_for_a_project_with_code_and_one_without():
    for has_code, phrase in ((True, "extend it"), (False, "workspace is empty")):
        prompt = bp.build_prompt("Lumen", "A status board", has_code)
        assert phrase in prompt and "Lumen" in prompt and "A status board" in prompt
        for must in (bp.PLAN_PATH, "ponytail", "frontend-design", "single-responsibility", "RUN.md", "exact commands", "ran successfully", "Not verified", ".env.example",
                     "Do not commit or push", "Assumptions"):
            assert must in prompt, must


def test_the_rules_file_names_the_skills_and_puts_single_responsibility_first_class():
    for must in ("ponytail", "frontend-design", "single-responsibility", "one job", "Never put a secret in a file"):
        assert must in bp.RULES


def test_the_rules_are_added_to_a_projects_own_file_and_taken_out_again():
    own = "# Our rules\n\nUse tabs."
    merged = bp.with_rules(own)
    assert merged.startswith("# Our rules\n\nUse tabs.") and bp.RULES_BEGIN in merged and bp.RULES_END in merged
    assert bp.without_rules(merged, True) == "# Our rules\n\nUse tabs.\n"


def test_a_project_with_no_rules_file_ends_with_none_and_one_that_had_an_empty_file_keeps_it():
    assert bp.without_rules(bp.with_rules(None), False) is None  # nothing of ours is left behind
    assert bp.without_rules(bp.with_rules(""), True) == ""
    assert bp.without_rules(None, False) is None


def test_what_muse_added_to_the_rules_file_itself_is_kept():
    edited = bp.with_rules("Ours.") + "\n## Added by Muse\nRun `make test` before committing.\n"
    out = bp.without_rules(edited, True)
    assert "Ours." in out and "Added by Muse" in out and bp.RULES_BEGIN not in out


def test_the_run_guide_pass_asks_for_commands_that_were_run():
    prompt = bp.run_guide_prompt("Lumen")
    assert "RUN.md" in prompt and "actually run" in prompt and "ran successfully" in prompt and "Not verified" in prompt


def test_the_skills_do_not_trip_the_bot_creation_wording_either():
    """The prompt goes to Muse, not a chat, but the same words once turned a brief into a bot: keep it clear of them."""
    from agents.naming import asks_for_new_bot
    assert not asks_for_new_bot(bp.build_prompt("Lumen", "A status board", True)) and not asks_for_new_bot(bp.RULES)
