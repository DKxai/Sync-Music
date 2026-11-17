import re
import unicodedata
from collections import defaultdict

def normalized(value: str | None) -> str:
    if not value:
        return ''
    value = unicodedata.normalize('NFKC', value).casefold()
    return re.sub(r'[^\w]+', ' ', value).strip()

def preview_score(title_a, artist_a, duration_a, title_b, artist_b, duration_b):
    """Candidate ranking only; not sufficient to authorize automatic writes."""
    score=0
    if normalized(title_a) == normalized(title_b) and normalized(title_a): score+=0.55
    if normalized(artist_a) == normalized(artist_b) and normalized(artist_a): score+=0.30
    if duration_a and duration_b and abs(duration_a-duration_b)<=2500: score+=0.15
    return round(score,3)


VIDEO_LABEL = re.compile(
    r'\s*(?:\(\s*{label}\s*\)|\[\s*{label}\s*\]|[-–—|]\s*{label})\s*$'.format(
        label=r'(?:official (?:music )?video|official (?:audio|lyric video)|music video|'
              r'lyric video|lyrics|mv chính thức|video chính thức|official m/v)'
    ), re.IGNORECASE,
)
FEATURE = re.compile(r'\s*[\[(]\s*(?:ft\.?|feat\.?|featuring)\s+([^\])]+)[\])]\s*$', re.IGNORECASE)
ARTIST_SEPARATOR = re.compile(r'\s*[,;]\s*|\s+(?:&|x|ft\.?|feat\.?|featuring)\s+', re.IGNORECASE)


def _title_and_feature(title):
    value = unicodedata.normalize('NFKC', title or '').strip()
    while (cleaned := VIDEO_LABEL.sub('', value)) != value and cleaned:
        value = cleaned
    featured = FEATURE.search(value)
    if featured and value[:featured.start()].strip():
        return value[:featured.start()].strip(), featured[1]
    return value, None


def catalog_title(title):
    return _title_and_feature(title)[0]


def _fold(value):
    value = normalized(value).replace('đ', 'd')
    return ''.join(char for char in unicodedata.normalize('NFD', value)
                   if not unicodedata.combining(char))


def _artists(value):
    return tuple(dict.fromkeys(
        artist for name in ARTIST_SEPARATOR.split(value or '')
        if (artist := normalized(re.sub(r'\s+-\s+Topic$', '', name, flags=re.IGNORECASE)))
    ))


def _catalog_score(track, title, artist, duration, isrc):
    if track.isrc and isrc:
        return 4 if track.isrc.upper() == isrc.upper() else 0
    source_title, source_feature = _title_and_feature(track.title)
    target_title, target_feature = _title_and_feature(title)
    left, right = _artists(track.artist), _artists(artist)
    if not source_title or not target_title or not left or not right:
        return 0
    left += _artists(source_feature)
    right += _artists(target_feature)
    gap = abs(track.duration_ms - duration) if track.duration_ms and duration else None
    if normalized(source_title) == normalized(target_title) and set(left) == set(right):
        if gap is None:
            return 1 if normalized(track.title) == normalized(title) else 0
        return 3 if gap <= 5000 else 0
    if gap is None or gap > 3500 or _fold(source_title) != _fold(target_title):
        return 0
    left, right = tuple(map(_fold, left)), tuple(map(_fold, right))
    left_set, right_set = set(left), set(right)
    if left_set == right_set or (left[0] == right[0] and (left_set <= right_set or right_set <= left_set)):
        return 2
    return 0


def catalog_match(track, candidates):
    ranked = defaultdict(list)
    for track_id, title, artist, duration, isrc in candidates:
        score = _catalog_score(track, title, artist, duration, isrc)
        if score:
            ranked[score].append((track_id, isrc))
    if not ranked:
        return None, 'insufficient' if candidates else 'not_found'
    best = ranked[max(ranked)]
    recordings = {isrc.upper() if isrc else track_id for track_id, isrc in best}
    return (best[0][0], None) if len(recordings) == 1 else (None, 'ambiguous')
