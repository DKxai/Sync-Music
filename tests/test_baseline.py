from app.sync.matcher import normalized

def test_unicode_text():
    assert normalized('Bài Hát')==normalized('bài hát')
