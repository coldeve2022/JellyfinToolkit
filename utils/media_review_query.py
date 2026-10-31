"""Pure file-level review scope shared by display, counts and bulk selection."""
from dataclasses import dataclass


def favorite_state(item):
    value = item.get('is_favorite')
    return 'favorite' if value is True else 'not_favorite' if value is False else 'unknown'


def playback_state(item):
    played = item.get('played')
    if played is True:
        return 'played'
    if played is not False:
        return 'unknown'
    return 'progress' if item.get('has_progress') or item.get('playback_ticks', 0) else 'unplayed'


def aggregate_review(items):
    """Work-level state derived from the same file-level states as ReviewQuery.

    A positive favorite wins; negative favorites require every copy to be known.
    All copies must be played/unplayed, while any unfinished progress wins.
    """
    items = list(items)
    favorites = [favorite_state(item) for item in items]
    playback = [playback_state(item) for item in items]
    favorite = ('favorite' if 'favorite' in favorites else
                'not_favorite' if favorites and all(s == 'not_favorite' for s in favorites)
                else 'unknown')
    played = ('played' if playback and all(s == 'played' for s in playback) else
              'progress' if 'progress' in playback else
              'unplayed' if playback and all(s == 'unplayed' for s in playback)
              else 'unknown')
    return favorite, played


@dataclass(frozen=True)
class ReviewQuery:
    words: tuple[str, ...] = ()
    conclusion: str = ''
    favorite: str = 'all'
    playback: str = 'all'

    @classmethod
    def from_values(cls, search='', conclusion='', favorite='all', playback='all'):
        return cls(tuple(str(search).casefold().split()), conclusion or '', favorite or 'all', playback or 'all')

    def matches(self, path, identifier, conclusion, reason, item):
        if self.conclusion and conclusion != self.conclusion:
            return False
        if self.favorite != 'all' and favorite_state(item) != self.favorite:
            return False
        if self.playback != 'all' and playback_state(item) != self.playback:
            return False
        if not self.words:
            return True
        fields = [path, identifier, reason, item.get('name', ''),
                  *(item.get('tags') or []), *(item.get('genres') or [])]
        text = ' '.join(str(value or '') for value in fields).casefold()
        return all(word in text for word in self.words)
