# Multi-Keyword Search Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow a single task's `keyword` field to contain multiple search keywords (newline or comma separated), so the scraper searches each keyword sequentially and merges results.

**Architecture:** The `keyword` field stays `str` in the database/model for backward compatibility. The scraper parses it into a list at runtime, searches each keyword sequentially within the same browser context, and deduplicates all results into a single result set. The first keyword is used as the "primary" keyword for result filename and storage grouping.

**Tech Stack:** Python (FastAPI + Playwright), Vue 3 + TypeScript, Pytest

---

### Task 1: Add `parse_keywords` utility and model helper

**Files:**
- Modify: `src/domain/models/task.py`

**Step 1: Add `parse_keywords` utility function**

Add this function alongside the existing `_normalize_keyword_values` in `task.py`:

```python
def parse_keywords(keyword: str) -> list[str]:
    """Parse a keyword string into a list of individual keywords.

    Supports newline or comma separation. Returns a deduplicated list.
    Empty lines and whitespace-only entries are filtered out.
    """
    if not keyword or not keyword.strip():
        return []
    raw_values = re.split(r"[\n,]+", keyword)
    seen: set[str] = set()
    result: list[str] = []
    for item in raw_values:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
```

Add a `@property` to `Task` model to expose parsed keywords:

```python
    @property
    def keywords(self) -> list[str]:
        """Return the keyword field parsed as a list of individual keywords."""
        return parse_keywords(self.keyword)
```

**Step 2: Verify the model still works**

Run: `python -c "from src.domain.models.task import Task, parse_keywords; print(parse_keywords('a7m4\nA7M4,sony'))"`
Expected: `['a7m4', 'sony']`

**Step 3: Commit**

```bash
git add src/domain/models/task.py
git commit -m "feat(task): add parse_keywords utility and Task.keywords property"
```

---

### Task 2: Modify scraper to iterate over multiple keywords

**Files:**
- Modify: `src/scraper.py` (lines ~450-470 and ~640-920)

**Step 1: Parse keywords list at the top of `scrape_xianyu`**

At line ~450, after `keyword = task_config["keyword"]`, add:

```python
    keywords = parse_keywords(keyword)
    if not keywords:
        print("错误: 任务没有有效的搜索关键词，跳过执行。")
        return 0
    primary_keyword = keywords[0]  # Used for result filename and dedup grouping
```

Add the import at the top of scraper.py:

```python
from src.domain.models.task import parse_keywords
```

**Step 2: Use `primary_keyword` for result filename and dedup loading**

Replace the existing lines that use `keyword` for result/storage purposes (lines ~471-473):

```python
    historical_snapshots = load_price_snapshots(primary_keyword)
    result_filename = build_result_filename(primary_keyword)
    processed_links = load_processed_link_keys(primary_keyword)
```

Update the `load_price_snapshots` log message (line ~474-477) to show all keywords:

```python
    if processed_links:
        print(f"LOG: 发现已存在结果集 {result_filename}，已加载 {len(processed_links)} 个历史商品用于去重。")
    else:
        print(f"LOG: 结果集 {result_filename} 当前为空，将写入新记录。")
    if len(keywords) > 1:
        print(f"LOG: 本次搜索包含 {len(keywords)} 个关键词: {', '.join(keywords)}")
```

**Step 3: Add keyword iteration loop inside `_run_scrape_attempt`**

Inside `_run_scrape_attempt`, around line ~620 (after page setup, before the search navigation), wrap the existing search+pagination logic in a keyword loop. The structure becomes:

```python
            # --- Keyword iteration: search each keyword sequentially ---
            for kw_index, current_keyword in enumerate(keywords):
                if len(keywords) > 1:
                    log_time(f"===== 搜索关键词 [{kw_index + 1}/{len(keywords)}]: {current_keyword} =====")
                elif len(keywords) == 1:
                    current_keyword = keywords[0]
                    log_time(f"===== 搜索关键词: {current_keyword} =====")

                # Navigate to search results page
                log_time("步骤 1 - 导航到搜索结果页...")
                params = {"q": current_keyword}
                search_url = f"https://www.goofish.com/search?{urlencode(params)}"
                log_time(f"目标URL: {search_url}")

                # ... existing navigation + response capture code (lines ~657-669) ...

                # ... existing page iteration loop (lines ~921-1144) ...
                # Inside the loop, use `current_keyword` instead of `keyword` for:
                #   - record_market_snapshots(keyword=current_keyword, ...)
                #   - ItemAnalysisJob(keyword=current_keyword, ...)
                #   - save_to_jsonl(record, current_keyword)

                # Add anti-crawl delay between keywords (skip after the last one)
                if kw_index < len(keywords) - 1:
                    log_time("[反爬] 切换下一个关键词前，执行随机延迟...")
                    await random_sleep(5, 10)
```

The key substitution points inside the page loop:
1. `record_market_snapshots(keyword=current_keyword, ...)` (line ~946)
2. `ItemAnalysisJob(keyword=current_keyword, ...)` (where the job is constructed)
3. Any other place where the old `keyword` variable was used for per-item operations

**Important:** The outer variables (`processed_links`, `processed_item_count`, `historical_snapshots`, `history_seen_item_ids`, `history_run_id`) remain shared across all keywords so dedup works globally.

**Step 4: Test the scraper logic manually**

Run: `python -c "from src.domain.models.task import parse_keywords; kw='AI简历生成器\n找工作助手\n简历优化工具'; print(parse_keywords(kw))"`
Expected: `['AI简历生成器', '找工作助手', '简历优化工具']`

**Step 5: Commit**

```bash
git add src/scraper.py
git commit -m "feat(scraper): support multiple search keywords per task"
```

---

### Task 3: Update spider_v2.py keyword parsing

**Files:**
- Modify: `spider_v2.py`

The `spider_v2.py` already has its own `normalize_keywords` function for `keyword_rules`. No changes needed here because `keyword` is passed as-is to `scrape_xianyu`, which now handles parsing internally.

However, add a log line after loading tasks to show parsed keywords:

At line ~191, after `print(f"-> 任务 '{task_conf['task_name']}' 已加入执行队列。")`:

```python
        kw_raw = task_conf.get('keyword', '')
        kw_list = normalize_keywords(kw_raw)
        if len(kw_list) > 1:
            print(f"   关键词: {', '.join(kw_list)}")
```

**Step 1: Add the log line**

**Step 2: Commit**

```bash
git add spider_v2.py
git commit -m "feat(spider): log multiple keywords per task"
```

---

### Task 4: Update web UI — change keyword input to Textarea

**Files:**
- Modify: `web-ui/src/components/tasks/TaskForm.vue`
- Modify: `web-ui/src/i18n/messages/zh-CN-extra.ts`
- Modify: `web-ui/src/i18n/messages/en-US-extra.ts`
- Modify: `web-ui/src/types/task.d.ts` (no change needed, `keyword` stays `string`)

**Step 1: Change the keyword `<Input>` to `<Textarea>` in TaskForm.vue**

At line ~268-270, replace:

```vue
<Label for="keyword" class="sm:text-right">{{ t('tasks.form.keyword') }}</Label>
<Input id="keyword" v-model="form.keyword" class="sm:col-span-3" :placeholder="t('tasks.form.keywordPlaceholder')" required />
```

With:

```vue
<Label for="keyword" class="sm:text-right">{{ t('tasks.form.keyword') }}</Label>
<div class="sm:col-span-3">
  <Textarea
    id="keyword"
    v-model="form.keyword"
    :placeholder="t('tasks.form.keywordPlaceholder')"
    :rows="3"
    required
  />
  <p class="mt-1 text-xs text-muted-foreground">{{ t('tasks.form.keywordHint') }}</p>
</div>
```

Also add the `Textarea` import if not already present (check the existing imports).

**Step 2: Update i18n — zh-CN**

In `zh-CN-extra.ts`, update the keyword-related labels (lines ~49-50):

```typescript
keyword: '搜索关键词',
keywordPlaceholder: '每行一个关键词，例如：\nAI简历生成器\n找工作助手\n简历优化工具',
keywordHint: '支持多个关键词，每行一个或用逗号分隔。每个关键词将分别搜索，结果合并去重。',
```

**Step 3: Update i18n — en-US**

In `en-US-extra.ts`, update the keyword-related labels (lines ~49-50):

```typescript
keyword: 'Search Keywords',
keywordPlaceholder: 'One keyword per line, e.g.:\nAI Resume Builder\nJob Search Assistant',
keywordHint: 'Supports multiple keywords (one per line or comma-separated). Each keyword is searched separately and results are merged.',
```

**Step 4: Update validation message**

In `zh-CN-extra.ts`, update validation message (line ~109):

```typescript
nameAndKeywordRequired: '任务名称和搜索关键词不能为空。',
```

In `en-US-extra.ts`:

```typescript
nameAndKeywordRequired: 'Task name and search keyword(s) are required.',
```

**Step 5: Build the frontend to verify no errors**

Run: `cd web-ui && npm run build`
Expected: Build succeeds with no errors.

**Step 6: Commit**

```bash
git add web-ui/src/components/tasks/TaskForm.vue web-ui/src/i18n/messages/zh-CN-extra.ts web-ui/src/i18n/messages/en-US-extra.ts
git commit -m "feat(web-ui): change keyword input to textarea for multi-keyword support"
```

---

### Task 5: Update existing tests and add new tests

**Files:**
- Modify: `tests/unit/test_keyword_rule_engine.py` (if keyword parsing tests are needed there)
- Create: `tests/unit/test_parse_keywords.py`

**Step 1: Write tests for `parse_keywords`**

```python
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
        """Regular commas work; Chinese commas (，) are NOT split by default."""
        result = parse_keywords("a7m4，sony")
        # Chinese comma is not in the split pattern, so it stays as one token
        assert result == ["a7m4，sony"]
```

**Step 2: Run tests**

Run: `pytest tests/unit/test_parse_keywords.py -v`
Expected: All tests pass.

**Step 3: Run existing tests to verify no regressions**

Run: `pytest tests/unit/ -v`
Expected: All existing tests still pass.

**Step 4: Commit**

```bash
git add tests/unit/test_parse_keywords.py
git commit -m "test: add unit tests for parse_keywords utility"
```

---

### Task 6: Build frontend and run full test suite

**Files:** No new files.

**Step 1: Build frontend**

Run: `cd web-ui && npm run build`
Expected: Build succeeds.

**Step 2: Run all tests**

Run: `pytest tests/unit/ -v`
Expected: All tests pass.

**Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: cleanup for multi-keyword search feature"
```
