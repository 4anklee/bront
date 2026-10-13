from bront.measurement import percentile


def test_percentiles_use_nearest_rank():
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([9, 1, 5], 50) == 5
    assert percentile([4], 99) == 4
