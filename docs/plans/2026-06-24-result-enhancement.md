# Plan: 结果页增强 — 成交量/发布时间筛选 + 复刻可行性评估

## Context

当前结果页的筛选能力有限（仅推荐状态 + 基础排序），AI 分析也只评估"是否值得购买"。用户有两个需求：

1. **更强的筛选能力**：按商品成交量（浏览量、想要人数）和发布时间筛选，快速定位高关注度、近期发布的商品
2. **复刻可行性评估**：用户是 AI 工程师，希望从闲鱼商品中筛选出可以用 AI 能力一次性复刻、反复售卖的数字产品（数字产品/服务、可复制数字资产、AI 辅助开发的小工具）。**不做外包代工，只做一次性产出的产品。**

两个需求共享大量相同的前端基础设施（FilterBar、composable、API 参数、i18n），适合合并实施。

---

## Feature 1: 成交量 & 发布时间筛选

### 需求

- **成交量筛选**：`浏览量` 和 `想要人数` 各提供一个最小值输入框（留空 = 不限）
- **发布时间筛选**：预设时间段下拉选择（不限 / 1天内 / 3天内 / 7天内 / 30天内 / 90天内）

### 技术决策：混合筛选策略

- **发布时间** → SQL WHERE 过滤（`publish_time` 是独立列，有索引）
- **浏览量 / 想要人数** → Python 后处理过滤（字段在 `raw_json` JSON blob 中，且 `"想要"人数"` 含 Unicode 智能引号 U+201C/U+201D，SQLite `json_extract` 无法解析。现有架构已在 Python 层做黑名单/可见性过滤，保持一致）

---

## Feature 2: 复刻可行性评估

### 需求

在现有 AI 分析流程中新增**复刻可行性评估**，AI 同时输出两套结论：
1. **购买评估**（现有 `is_recommended` + `criteria_analysis`）
2. **复刻评估**（新增 `replication_assessment`）

### AI 输出结构新增字段

在现有 `ai_analysis` 响应中新增 `replication_assessment` 对象，`prompt_version` 升级到 V6.5：

```json
{
  "prompt_version": "EagleEye-V6.5",
  "is_recommended": true,
  "reason": "购买评估理由...",
  "risk_tags": [...],
  "criteria_analysis": { ... },

  "replication_assessment": {
    "is_replicable": true,
    "replication_reason": "用 Claude 可在 2 小时内完成，闲鱼售价 19.9，成本约 0.5 元",
    "replication_score": 82,
    "feasibility": {
      "status": "PASS",
      "comment": "AI 写代码+生成图片即可实现",
      "evidence": "商品为 AI 写真服务，使用 ComfyUI + SDXL 可复刻"
    },
    "cost": {
      "status": "PASS",
      "one_time_effort": "低",
      "ai_cost_estimate": "低",
      "comment": "模板搭建一次，后续零边际成本"
    },
    "pricing": {
      "status": "PASS",
      "estimated_price": "9.9-29.9",
      "market_demand": "中高",
      "comment": "闲鱼同类产品售价 15-30，需求稳定"
    },
    "moat": {
      "status": "NEEDS_MANUAL_CHECK",
      "comment": "技术门槛低但运营门槛中等，需要持续获客",
      "evidence": "同类卖家较多，差异化靠包装和文案"
    }
  }
}
```

### Prompt 设计 — 复刻评估分析原则（Part C）

在 criteria 文件中新增 **Part C**（与现有 Part A 产品评估、Part B 卖家评估并列）：

> **角色设定**：你是一个资深的 AI 工程师和独立开发者，擅长用 AI 工具（大语言模型、图片生成、音频生成等）快速产出数字产品并在闲鱼等平台销售。
>
> **评估目标**：判断该商品是否适合用 AI 能力一次性复刻并在闲鱼出售。只考虑"一次性产出、可反复售卖"的数字产品，不考虑外包代工或持续服务。
>
> **C1. 技术可行性 (feasibility)**
> - 判断当前主流 AI 工具链（代码生成、图片/音频/视频生成、模板工具等）能否实现该产品
> - 考虑实现难度：纯 AI 生成 > AI + 简单模板 > AI + 需要较多人工调试
> - 如果需要非 AI 的专业技能（如专业设计、复杂编程），标注为 NEEDS_MANUAL_CHECK
>
> **C2. 一次性成本 (cost)**
> - `one_time_effort`：从零到成品的总工时（低 < 2h / 中 2-8h / 高 > 8h）
> - `ai_cost_estimate`：AI API 调用成本（低 < 5 元 / 中 5-20 元 / 高 > 20 元）
> - 总成本 = 工时 × 时薪 + AI 费用，和预期售价比较
>
> **C3. 定价空间 (pricing)**
> - 参考闲鱼同类产品的定价区间
> - `market_demand`：根据搜索结果数量和卖家数量判断（高/中高/中/低）
> - 评估售价是否足以覆盖成本并有合理利润（建议利润率 > 50%）
>
> **C4. 竞争壁垒 (moat)**
> - 评估复刻难度：如果任何人用 AI 都能轻松复刻，壁垒为 FAIL
> - 考虑差异化空间：包装、文案、bundling、细分定位
> - 技术门槛低但运营/营销能力可形成壁垒的，标注 NEEDS_MANUAL_CHECK
>
> **综合判定逻辑**：
> - 四项都 PASS → `is_replicable: true`，`replication_score` 70-100
> - 三项 PASS + 一项 NEEDS_MANUAL_CHECK → `is_replicable: true`，`replication_score` 50-79
> - 两项及以下 PASS → `is_replicable: false`，`replication_score` 0-49
> - `replication_score` 权重：可行性 30%、成本 25%、定价 25%、壁垒 20%

### 前端展示

1. **ResultCard 徽章**：可复刻商品显示绿色"可复刻"标记 + `replication_score` 分数，hover 显示 `replication_reason`
2. **ResultCard 折叠面板**：AI 分析区域下方新增"复刻评估"面板，展示四维度状态和评语
3. **筛选栏**：`is_replicable_only` checkbox + `min_replication_score` 数字输入框 + `replication_score` 排序选项
4. **和现有筛选的关系**：复刻筛选与购买/成交量/发布时间筛选互不冲突，可任意组合

---

## 修改文件清单

按文件聚合（标明每个文件涉及哪些 Feature），避免重复修改。

### Prompt 层

| 文件 | Feature | 改动 |
|------|---------|------|
| `prompts/base_prompt.txt` | F2 | JSON 输出格式新增 `replication_assessment` 结构；`prompt_version` 升级到 V6.5 |
| `prompts/macbook_criteria.txt` | F2 | 新增 Part C 复刻评估分析原则（上文完整内容） |
| `prompts/*_criteria.txt`（其他） | F2 | 同样新增 Part C |

### 后端层

| 文件 | Feature | 改动 |
|------|---------|------|
| `src/services/result_storage_service.py` | F1 | `_build_query_conditions()` 新增 `publish_within_days` SQL WHERE；`_load_filtered_records_from_conn()` 新增 `min_view_count`/`min_want_count` Python 后处理过滤；`query_result_records()`/`load_all_result_records()` 透传 3 个新参数 |
| `src/api/routes/results.py` | F1+F2 | GET `/{filename}` 和 `/{filename}/export` 新增 5 个 Query 参数：`min_view_count`、`min_want_count`、`publish_within_days`、`is_replicable_only`、`min_replication_score` |
| `src/ai_handler.py` | F2 | `validate_ai_response_format()` 新增 `replication_assessment` 可选字段验证 |

### 前端层

| 文件 | Feature | 改动 |
|------|---------|------|
| `web-ui/src/types/result.d.ts` | F2 | 新增 `ReplicationAssessment` 接口 |
| `web-ui/src/api/results.ts` | F1+F2 | `GetResultContentParams` 新增 `min_view_count`/`min_want_count`/`publish_within_days`/`is_replicable_only`/`min_replication_score` |
| `web-ui/src/composables/useResults.ts` | F1+F2 | `loadPersistedFilters()` defaults 新增 5 个字段的默认值 |
| `web-ui/src/components/results/ResultsFilterBar.vue` | F1+F2 | 新增一行 3 列 grid（浏览量/想要人数输入框 + 发布时间 Select）；checkbox 行新增"仅看可复刻"；同一行新增"最低复刻分数"输入框；排序下拉新增 `replication_score` |
| `web-ui/src/components/results/ResultCard.vue` | F2 | 新增"可复刻"徽章 + 复刻评估折叠面板 |
| `web-ui/src/views/ResultsView.vue` | F1+F2 | `<ResultsFilterBar>` 新增所有新字段的 v-model 绑定 |
| `web-ui/src/i18n/messages/zh-CN.ts` + `en-US.ts` | F1+F2 | 新增翻译键（见下表） |

### i18n 新增翻译

**Feature 1 — 筛选：**

| Key | zh-CN | en-US |
|---|---|---|
| `results.filters.minViewCount` | 最低浏览量 | Min. Views |
| `results.filters.minViewCountPlaceholder` | 不限 | Any |
| `results.filters.minWantCount` | 最低想要人数 | Min. Wants |
| `results.filters.minWantCountPlaceholder` | 不限 | Any |
| `results.filters.publishTime` | 发布时间 | Publish Time |
| `results.filters.publishAll` | 不限 | Any |
| `results.filters.publish1Day` | 1天内 | Within 1 Day |
| `results.filters.publish3Days` | 3天内 | Within 3 Days |
| `results.filters.publish7Days` | 7天内 | Within 7 Days |
| `results.filters.publish30Days` | 30天内 | Within 30 Days |
| `results.filters.publish90Days` | 90天内 | Within 90 Days |

**Feature 2 — 复刻评估：**

| Key | zh-CN | en-US |
|---|---|---|
| `results.filters.replicableOnly` | 仅看可复刻 | Replicable Only |
| `results.filters.minReplicationScore` | 最低复刻分数 | Min. Replication Score |
| `results.filters.minReplicationScorePlaceholder` | 不限 | Any |
| `results.filters.sortByReplicationScore` | 按复刻分数 | By Replication Score |
| `results.card.replicable` | 可复刻 | Replicable |
| `results.card.replicationScore` | 复刻分数 | Replication Score |
| `results.card.replicationDetail` | 复刻评估 | Replication Assessment |
| `results.card.feasibility` | 技术可行性 | Technical Feasibility |
| `results.card.cost` | 一次性成本 | One-time Cost |
| `results.card.pricing` | 定价空间 | Pricing Space |
| `results.card.moat` | 竞争壁垒 | Competitive Moat |

---

## 实现顺序

### Phase 1: 筛选基础设施（F1）

前后端筛选管道搭建，为 F2 的复刻筛选复用同一套基础设施。

1. **后端核心** — `result_storage_service.py`（SQL WHERE + Python 过滤）
2. **后端 API** — `results.py`（新增 F1 的 3 个 Query 参数）
3. **前端类型 + 状态** — `results.ts` + `useResults.ts`（接口 + 默认值）
4. **前端 UI** — `ResultsFilterBar.vue` + `ResultsView.vue`（浏览量/想要人数/发布时间筛选控件）
5. **i18n** — F1 翻译键

### Phase 2: 复刻评估（F2）

在 Phase 1 的筛选基础设施上叠加复刻评估。

1. **Prompt 核心** — `base_prompt.txt`（输出结构）+ criteria 文件（Part C 分析原则）
2. **后端验证** — `ai_handler.py`（可选字段验证）
3. **后端 API** — `results.py`（追加 F2 的 2 个 Query 参数 + Python 后处理过滤）
4. **前端类型** — `result.d.ts`（`ReplicationAssessment` 接口）
5. **前端状态** — `useResults.ts`（追加默认值）
6. **前端 UI** — `ResultsFilterBar.vue`（复刻筛选控件）+ `ResultCard.vue`（徽章 + 折叠面板）
7. **i18n** — F2 翻译键

---

## 验证方式

### Feature 1 验证

1. **后端测试**：构造含不同浏览量、想要人数、发布时间的测试数据，验证筛选逻辑
2. **手动测试**：启动前后端，在结果页输入筛选条件，确认结果正确
3. **导出测试**：应用筛选后导出 CSV，确认导出内容与页面筛选一致
4. **持久化测试**：设置筛选条件后刷新页面，确认 localStorage 恢复了筛选状态

### Feature 2 验证

5. **Prompt 验证**：手动用几条真实闲鱼商品数据调用 AI，检查 `replication_assessment` 输出结构和内容质量
6. **兼容性测试**：旧数据（无 `replication_assessment`）不应导致前端报错，徽章不显示即可
7. **筛选测试**：勾选"仅看可复刻"、设置最低复刻分数、按复刻分数排序，验证结果正确
8. **组合测试**：同时启用 F1 筛选 + F2 筛选，验证组合过滤正确
