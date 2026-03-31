DEFAULT_K_FACTOR = 32
PROVISIONAL_K_FACTOR = 64
PROVISIONAL_THRESHOLD = 10  # games played before K-factor drops


def get_k_factor(games_played: int) -> int:
    if games_played < PROVISIONAL_THRESHOLD:
        return PROVISIONAL_K_FACTOR
    return DEFAULT_K_FACTOR


def expected_score(rating_a: int, rating_b: int) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def calculate_new_ratings(
    winner_elo: int, loser_elo: int,
    winner_games: int = PROVISIONAL_THRESHOLD,
    loser_games: int = PROVISIONAL_THRESHOLD,
) -> tuple[int, int]:
    expected_win = expected_score(winner_elo, loser_elo)
    expected_lose = expected_score(loser_elo, winner_elo)

    winner_k = get_k_factor(winner_games)
    loser_k = get_k_factor(loser_games)

    new_winner_elo = round(winner_elo + winner_k * (1 - expected_win))
    new_loser_elo = round(loser_elo + loser_k * (0 - expected_lose))

    return new_winner_elo, new_loser_elo
