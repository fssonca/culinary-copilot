from culinary_copilot.recipes.exact_duplicates import deduplicate_exact_sources


def test_exact_source_only_and_all_provenance_preserved():
    records = [
        dict(source_id=str(n), content_hash="same", provenance={"attempt": n}) for n in range(3)
    ]
    unique, aliases = deduplicate_exact_sources(
        records, {"0": "2 cups flour", "1": "2 cups flour", "2": "3 cups flour"}
    )
    assert len(unique) == 2
    assert len(aliases) == 3
    assert [r["source_id"] for r in unique[0]["source_records"]] == ["0", "1"]
    assert unique[0]["duplicate_interpretations"][0]["provenance"] == {"attempt": 1}
    assert "source_records" not in records[0]
