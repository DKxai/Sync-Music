import re
import unicodedata

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
