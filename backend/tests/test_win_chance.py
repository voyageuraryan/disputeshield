from decimal import Decimal

from app.agents.analyst import win_chance_problems


def test_stated_win_chance_must_match_the_calculator():
    live = "With a 0% chance to win the fight and a net loss of $45 if settled, accepting the claim is safest."
    assert win_chance_problems([live], Decimal("0.15"))  # the live contradiction (Options card said 15%)
    assert not win_chance_problems(["Only a 15% chance to win; settling is cheaper."], Decimal("0.15"))
    assert not win_chance_problems(["a 15.4% estimated chance of winning"], Decimal("0.15"))
    assert not win_chance_problems(["the low win chance makes fighting expensive"], Decimal("0.15"))  # no number
    assert not win_chance_problems([live], None)  # no fight option: nothing to compare
    assert win_chance_problems(["", "an 80% probability to win"], Decimal("0.43"))
