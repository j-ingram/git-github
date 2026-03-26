K_FACTOR = 32


def expected_score(rating_a: int, rating_b: int) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def calculate_new_ratings(
    winner_elo: int, loser_elo: int
) -> tuple[int, int]:
    expected_win = expected_score(winner_elo, loser_elo)
    expected_lose = expected_score(loser_elo, winner_elo)

    new_winner_elo = round(winner_elo + K_FACTOR * (1 - expected_win))
    new_loser_elo = round(loser_elo + K_FACTOR * (0 - expected_lose))

    return new_winner_elo, new_loser_elo
