"""[[SPAWN]] parsing: block form, legacy one-liner, and markers that must not be taken for either."""
from markers.spawns import parse_spawns


def test_block_with_and_without_an_old_tier_part():
    for head in ("Scout", "Scout | JUDGEMENT"):
        clean, spawns = parse_spawns(f"hi\n[[SPAWN: {head}]]\nrole a\n[[/SPAWN]]")
        assert (clean, spawns) == ("hi", [{"name": "Scout", "system_prompt": "role a"}])


def test_one_liner_keeps_its_instructions():
    _, spawns = parse_spawns("[[SPAWN: Bob | do X | FAST]]")
    assert spawns == [{"name": "Bob", "system_prompt": "do X"}]


def test_one_liner_before_a_block_does_not_steal_its_closer():
    _, spawns = parse_spawns("[[SPAWN: A | role | FAST]] mid [[SPAWN: B]]\nrole b\n[[/SPAWN]]")
    assert spawns == [{"name": "B", "system_prompt": "role b"}, {"name": "A", "system_prompt": "role"}]


def test_marker_without_a_closer_is_not_a_spawn():
    clean, spawns = parse_spawns("x [[SPAWN: broken]] oops")
    assert spawns == [] and "[[SPAWN: broken]]" in clean


def test_stray_start_does_not_swallow_the_next_one_liner():
    clean, spawns = parse_spawns("x [[SPAWN: broken]] oops\n[[SPAWN: Bob | do X | FAST]]")
    assert spawns == [{"name": "Bob", "system_prompt": "do X"}]
    assert clean == "x [[SPAWN: broken]] oops"
