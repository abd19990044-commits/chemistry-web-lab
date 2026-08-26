# -*- coding: utf-8 -*-
"""
Tests for Kaggle Dataset and Basis Set URL Normalization and Validation.

Verifies:
- Complete Kaggle URLs (with https, http, www, subdomains) normalize to 'owner/dataset-slug'
- Query parameters, hash anchors, trailing slashes, and subpaths are cleanly stripped
- Already-normalized 'owner/dataset-slug' identifiers are preserved verbatim
- Whitespace and quotes are stripped cleanly
- Unrelated URLs (e.g., google.com, github.com) and malformed inputs are rejected with clear ValueError
- Multi-dataset comma-separated strings and lists are deduplicated and normalized
"""
import pytest
from kaggle_runner import clean_dataset_source, clean_dataset_sources


class TestKaggleDatasetUrlNormalization:
    """Unit test suite for clean_dataset_source and clean_dataset_sources."""

    @pytest.mark.parametrize(
        "raw_input,expected",
        [
            ("https://www.kaggle.com/datasets/jon534/orca6", "jon534/orca6"),
            ("http://www.kaggle.com/datasets/jon534/orca6", "jon534/orca6"),
            ("https://kaggle.com/datasets/jon534/orca6", "jon534/orca6"),
            ("kaggle.com/datasets/jon534/orca6", "jon534/orca6"),
            ("www.kaggle.com/datasets/jon534/orca6", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/jon534/orca6/", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/jon534/orca6?tab=activity", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/jon534/orca6#files", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/jon534/orca6/settings", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/jon534/orca6/versions/1", "jon534/orca6"),
            ("https://www.kaggle.com/jon534/orca6", "jon534/orca6"),
            ("jon534/orca6", "jon534/orca6"),
            ("   jon534/orca6   ", "jon534/orca6"),
            ('"jon534/orca6"', "jon534/orca6"),
            ("'jon534/orca6'", "jon534/orca6"),
            ("https://www.kaggle.com/datasets/alice_smith/orca-6.0.0-linux_x86-64", "alice_smith/orca-6.0.0-linux_x86-64"),
            ("alice_smith/orca-6.0.0-linux_x86-64", "alice_smith/orca-6.0.0-linux_x86-64"),
        ],
    )
    def test_valid_dataset_url_normalization(self, raw_input: str, expected: str):
        assert clean_dataset_source(raw_input) == expected

    def test_clean_dataset_sources_list_and_string(self):
        # Comma-separated string with mixed URLs and slugs
        raw_str = "https://www.kaggle.com/datasets/user1/dataset-a, user2/dataset-b, https://kaggle.com/user1/dataset-a"
        cleaned = clean_dataset_sources(raw_str)
        assert cleaned == ["user1/dataset-a", "user2/dataset-b"]

        # List input with trailing/leading spaces
        raw_list = [
            " https://www.kaggle.com/datasets/owner/ds1?view=table ",
            "owner/ds2",
            "owner/ds1",
        ]
        cleaned_list = clean_dataset_sources(raw_list)
        assert cleaned_list == ["owner/ds1", "owner/ds2"]

    @pytest.mark.parametrize(
        "invalid_input",
        [
            "https://www.google.com/search?q=kaggle",
            "https://github.com/owner/repository",
            "https://www.kaggle.com/",
            "https://www.kaggle.com/datasets",
            "https://www.kaggle.com/datasets/",
            "https://kaggle.com/code/user/notebook",
            "just_a_single_word",
            "invalid/name with spaces/more",
            "invalid owner/dataset slug with spaces",
        ],
    )
    def test_invalid_dataset_inputs_rejected(self, invalid_input: str):
        with pytest.raises(ValueError):
            clean_dataset_source(invalid_input)

    def test_empty_input_returns_empty(self):
        assert clean_dataset_source("") == ""
        assert clean_dataset_source(None) == ""
        assert clean_dataset_sources([]) == []
