"""Tests for parse_keywords utility."""
import pytest
from src.domain.models.task import parse_keywords


class TestParseKeywords:
    def test_single_keyword(self):
        assert parse_keywords("a7m4") == ["a7m4"]

    def test_newline_separated(self):
        result = parse_keywords("AI简历生成器\n找工作助手\n简历优化工具")
        assert result == ["AI简历生成器", "找工作助手", "简历优化工具"]

    def test_comma_separated(self):
        result = parse_keywords("a7m4,sony,canon")
        assert result == ["a7m4", "sony", "canon"]

    def test_mixed_separators(self):
        result = parse_keywords("a7m4\nsony,canon")
        assert result == ["a7m4", "sony", "canon"]

    def test_deduplication(self):
        result = parse_keywords("a7m4\na7m4\nA7M4")
        assert result == ["a7m4"]

    def test_empty_lines_filtered(self):
        result = parse_keywords("a7m4\n\n\nsony")
        assert result == ["a7m4", "sony"]

    def test_whitespace_trimmed(self):
        result = parse_keywords("  a7m4  \n  sony  ")
        assert result == ["a7m4", "sony"]

    def test_empty_string(self):
        assert parse_keywords("") == []

    def test_none_input(self):
        assert parse_keywords(None) == []

    def test_whitespace_only(self):
        assert parse_keywords("  \n  ") == []

    def test_chinese_comma(self):
        """Chinese fullwidth commas (，) are split just like ASCII commas."""
        result = parse_keywords("a7m4，sony")
        assert result == ["a7m4", "sony"]

    def test_chinese_dunhao(self):
        """Chinese enumeration commas (、) are split."""
        result = parse_keywords("a7m4、sony、canon")
        assert result == ["a7m4", "sony", "canon"]
