from llm.routing import plain_question

TEAM = "- Dev: backend, infrastructure\n- Ana: mobile UI"


def test_plain_question_skips_router():
    assert plain_question("What does the roadmap look like?", TEAM)
    assert not plain_question("What is the mobile plan?", TEAM)  # names a teammate's field
    assert not plain_question("Thanks, that is great", TEAM)  # no reaction would be given
    assert not plain_question("How about you add a login page", TEAM)  # not a question
