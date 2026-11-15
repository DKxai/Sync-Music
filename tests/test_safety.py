from app.sync.safety import deletion_budget, deletion_is_safe
from app.sync.matcher import preview_score

def test_budget_small():
    assert deletion_budget(5)==1
    assert deletion_is_safe(5,1)
    assert not deletion_is_safe(5,2)

def test_budget_large():
    assert deletion_budget(1000)==20
    assert not deletion_is_safe(1000,21)

def test_matcher():
    assert preview_score('Song','Artist',210000,'Song','Artist',211000)==1.0
