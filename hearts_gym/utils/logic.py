import enum

from hearts_gym.envs.hearts_game import HeartsGame
import numpy as np
from typing import Optional, Tuple, Iterable, List

from hearts_gym.envs.card_deck import Card


class Probability(float):
    def __init__(self, value) -> None:
        if not 0 <= value <= 1:
            raise ValueError(f"Invalid probability value of {value} encountered.")
        super().__init__()


class Certainty:
    """The NEVER and ALWAYS can be treated as probabilities!"""
    MAYBE = -1
    NEVER = Probability(0)
    ALWAYS = Probability(1)


ALWAYS = Certainty.ALWAYS
MAYBE = Certainty.MAYBE
NEVER = Certainty.NEVER

CARDS = tuple(
    Card(s, r)
    for s in range(Card.NUM_SUITS)
    for r in range(Card.NUM_RANKS)
)

class Player(enum.IntEnum):
    US = 0
    P1 = 1
    P2 = 2
    P3 = 3

    @staticmethod
    def from_offset(offset: int):
        return Player(offset % 4)


class Ownerships:
    # TODO: implement Ownership tracking over rounds

    def __init__(self, probs: np.ndarray) -> None:
        self.probs = probs
        super().__init__()

    def has_suit(self, player: Player, suit: int) -> Probability:
        return Probability(1 - np.prod([
            1 - p
            for c, p in zip(CARDS, self.probs[:, player])
            if c.suit == suit
        ]))

    def has_card_above(self, player: Player, card: Card) -> Probability:
        return Probability(1 - np.prod([
            1 - p
            for c, p in zip(CARDS, self.probs[:, player])
            if c.suit == card.suit and c.rank > card.rank
        ]))

    def has_card(self, player: Player, card: Card) -> Probability:
        return Probability(self.probs[CARDS.index(card), player])

    @staticmethod
    def from_trick(*, hand: Iterable[Card], trick: Iterable[Card], played: Iterable[Card], unseen: Iterable[Card]):
        """
        Infer ownerships of cards without knowing the history of the game.

        Can be used to create the Ownership object in the first round.
        """
        played = set(played)
        table = set(trick)
        hand = set(hand)
        unseen = set(unseen)
        assert played | table | hand | unseen == set(CARDS)

        probs = np.zeros((Card.NUM_SUITS * Card.NUM_RANKS, 4), dtype=float)
        for c, card in enumerate(CARDS):
            if card in hand:
                probs[c] = [1, 0, 0, 0]
            elif card in table or card in played:
                probs[c] = 0
            else:
                # TODO: 1/3 is only correct if the table is empty
                probs[c] = [0, 1/3, 1/3, 1/3]
        return Ownerships(probs)


class DeepState:
    def __init__(self, game) -> None:
        self.game = game
        self.cards_on_hand = np.array(self.game.hand)
        self.cards_on_table = np.array(self.game.table_cards)
        self.unseen_cards = self.game.unknown_cards
        self.cards_by_others = [c for c in self.unseen_cards if c not in self.cards_on_hand]
        self.legal_indices_to_play = np.array(self.game.get_legal_actions())
        self.legal_cards_to_play = [self.cards_on_hand[i] for i in self.legal_indices_to_play]
        self.penalty_on_table = np.array([self.game.get_penalty(c) for c in self.cards_on_table])
        self.penalty_of_action_cards = np.array([self.game.get_penalty(c) for c in self.legal_cards_to_play])
        self.T = len(self.cards_on_table)
        self.H = len(self.cards_on_hand)
        self.A = len(self.legal_indices_to_play)

        assert np.shape(self.legal_cards_to_play) == (self.A,), f"shape was {np.shape(self.penalty_of_action_cards)}"
        assert np.shape(self.legal_indices_to_play) == (self.A,), f"shape was {np.shape(self.legal_indices_to_play)}"
        assert np.shape(self.penalty_on_table) == (self.T,), f"shape was {np.shape(self.penalty_on_table)}"
        assert np.shape(self.penalty_of_action_cards) == (self.A,), f"shape was {np.shape(self.penalty_of_action_cards)}"

        super().__init__()

    def calculate_get_avoid_probabilities(self) -> Tuple[np.ndarray, np.ndarray]:
        p_get_trick = np.repeat(np.nan, self.A)
        p_avoid_trick = np.repeat(np.nan, self.A)
        inbound_penalty = np.repeat(np.nan, self.A)
        for a, c in enumerate(self.legal_cards_to_play):
            # Would this card get the trick?
            gets, expected_penalty = gets_trick(c, self.cards_on_table, self.cards_by_others)
            p_get_trick[a] = gets
            p_avoid_trick[a] = 1 - gets
            inbound_penalty[a] = expected_penalty

        assert not any(np.isnan(p_get_trick))
        assert not any(np.isnan(p_avoid_trick))
        assert not any(np.isnan(inbound_penalty))
        return p_get_trick, p_avoid_trick, inbound_penalty


def gets_trick(card: Card, table_cards: Iterable[Card], cards_by_others: Iterable[Card]) -> Tuple[Probability, float]:
    """
    Determines the certainty of a given `card` "winning" a trick,
    based on cards on the table and cards of other players.
    """
    lead_suit = table_cards[0].suit if len(table_cards) else card.suit
    lead_rank = table_cards[0].rank if len(table_cards) else card.rank

    if card.suit != lead_suit:
        return NEVER, 0
    # We are qualified based on suit!

    # We have established that our card has the correct suit.
    # Are there cards on the table that are ranked higher?
    table_cards_that_beat_this = filter_cards_above(table_cards, card.suit, card.rank)
    if table_cards_that_beat_this:
        return NEVER, 0

    if len(table_cards) == 3:
        # We are playing the last card! This is easy.
        # 1. There are no table cards that are higher.
        # 2. There are no cards still coming in.
        return ALWAYS, 0

    # Based on unplayed cards by competitors...
    n_incoming = 4 - len(table_cards) - 1
    other_cards_above = filter_cards_above(cards_by_others, card.suit, card.rank)
    other_cards_below = filter_cards_below(cards_by_others, card.suit, card.rank)

    # Cards above ours can't give us a penalty!
    penalties_below = tuple(map(HeartsGame.get_penalty, other_cards_below))
    if penalties_below:
        expected_incoming_penalty = np.mean(penalties_below) * n_incoming
    else:
        expected_incoming_penalty = 0

    # Trivial case first:
    if not other_cards_above:
        return ALWAYS, expected_incoming_penalty
    
    if len(cards_by_others) < 4:
        # This is the last tick, so that one competitor card that beats us WILL be played.
        return NEVER, 0

    # More than one round is left and there are _some_ competitor cards that could beat us.
    # The following probability calculation is less than ideal in at least the following ways:
    # - We don't have probabilities of other players holding onto certain cards, that could further
    #   constrain the potential outcomes.
    # - We don't know which rules the other agents use for their decision making.
    # - Because we don't know which players hold onto the remaining cards, we can't filter down to only the
    #   cards that could "legally" be played.
    # - The "unplayed cards" also include cards by players that have already played in this trick.

    # The tradeoff:
    # Assume that the upcoming cards will be drawn randomly from the remaining cards.

    # Find out what's the penalty we have to take IF we have to take the trick.
    probability_of_getting = p_gets_trick(
        n_others_higher=len(other_cards_above),
        n_others_lower=len(other_cards_below),
        n_incoming=n_incoming,
    )
    return probability_of_getting, expected_incoming_penalty


def filter_cards_above(cards: Iterable[Card], ref_suit:int, ref_rank: int) -> Tuple[Card]:
    """Returns all elements from `cards` that have the same suit, and a higher rank."""
    return tuple(
        c
        for c in cards
        if c.suit == ref_suit and c.rank > ref_rank
    )


def filter_cards_below(cards: Iterable[Card], ref_suit:int, ref_rank: int) -> Tuple[Card]:
    """Returns all elements from `cards` that have the same suit, and a higher rank."""
    return tuple(
        c
        for c in cards
        if c.suit != ref_suit or c.rank < ref_rank
    )


def p_gets_trick(
    n_others_higher: int,
    n_others_lower: int,
    n_incoming: int,
) -> Probability:
    if n_incoming == 0:
        raise ValueError("Useless probability calculation. Probabily an upstream logic error.")
    p_lower = n_others_lower / (n_others_lower + n_others_higher)
    if p_lower == 1:
        return ALWAYS
    if p_lower == 0:
        return NEVER
    return p_lower ** n_incoming


def expected_inbound_penalty(
    card_penalties: np.ndarray,
    n_inbound: int,
) -> np.ndarray:
    """Elementwise contributions of penalty after drawing without replacement `n_inbound` times."""
    card_penalties = np.array(card_penalties)
    p_drawn = n_inbound / len(card_penalties)
    return card_penalties * p_drawn


def get_card_values(
    cards_of_interest: List[Card],
    cards_on_hand: List[Card],
    cards_on_table: List[Card],
    cards_by_others: List[Card],
) -> np.ndarray:
    values = np.linspace(0.1, -0.1, len(cards_of_interest))
    for i, c in enumerate(cards_of_interest):
        # Rule 1: Never lead with an Ace
        if len(cards_on_table) == 0 and c.rank == Card.RANKS.index("A"):
            # Increasing the "value" of the card means that it'll be less like to be played.
            values[i] += 0.5

        # Rule 2: Hold onto low hearts
        # This is already applied via linspace ranking from above.

        # Rule 3: Try keeping the ♥A.
        if c.suit in { Card.SUIT_HEART } and c.rank == Card.RANKS.index("A"):
            values[i] += 0.5

        # Rule 4: Void ♠ or 💎 early.
        if c.suit in { Card.SUIT_CLUB, Card.SUIT_DIAMOND}:
            # Moving them down by 0.1 will put them below cards of other suits.
            values[i] -= 0.2
    return values