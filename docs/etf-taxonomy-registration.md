# 大板块产业归属登记

## 什么时候需要登记

成员数不少于 300 的板块，成员重合度分不开对口与不对口的指数。这类板块的配对只有登记了产业归属才允许新开仓，
未登记的状态为 `BROAD_SECTOR_UNATTRIBUTED`，在 QQ 和工作台显示「大板块待产业归属」，仍可作为观察项。
规则见 [ETF V3.2 调测说明](etf-v3.2.md) 的「大板块入场约束」。

出现下列情况时登记：

- 研判里出现新的大板块，且它是明确的产业或主题方向。
- 已登记板块有新的对口指数进入产品目录。
- 已登记指数的编制方案改版，原依据不再成立（此时删除该行）。

300 只以下的板块不需要登记，登记只影响同源聚合。减仓、清仓和已有周期不受登记影响。

## 审核口径

逐行审核，一行是一个「板块 ↔ 指数」。全部满足才通过：

1. 板块是明确的产业或主题方向。概念、地域、状态标签（如超跌、华为概念、外贸）不登记。
2. 指数是境内 A 股行业或主题指数。宽基、跨境、港股通、综合类指数（如科创信息、TMT）不通过。
3. 官方编制方案正文明确点名该板块的主题。行业比板块宽但未点名的不通过（如计算机、通信设备之于算力）。
4. 指数覆盖板块的主要产业链。只覆盖一段的细分指数（如半导体材料设备、芯片设计）不通过。
5. 编制方案正文实际读过，记录文件 SHA-256 和官方地址。只有通用方案或取不到方案的不通过。

依据不能来自 ETF 名称或暴露度。大板块的暴露度没有区分力：2026-11-20 算力板块对中证全指家用电器的暴露是 70.6%。

## 取证

中证和上证指数用官方接口查编制方案地址，再下载：

```bash
curl -s "https://www.csindex.com.cn/csindex-home/indexInfo/index-details-data?fileLang=2&indexCode=931719"
sha256sum 931719.pdf
```

返回的 `编制方案[].filePath` 是中文方案地址。直接拼地址下载到 201,426 字节的图片表示该地址没有文件，不能当作依据。

## 归属文件

```json
{
  "version": "etf-taxonomy-v3.2-r3-dividend",
  "rows": [
    {
      "sector_id": "kaipanla:801004",
      "index_code": "931719.CSI",
      "family_id": "battery",
      "canonical_branch_id": "lithium_battery",
      "available_at": "2026-11-20T12:38:53.937511+08:00",
      "evidence_refs": ["<编制方案 SHA-256>", "<官方地址>"]
    }
  ]
}
```

- 发布是整份替换。已发布的行原样保留，只追加或删除，不改写已有行的 `available_at` 和依据。
- 内容变化必须换新的 `version`。
- `index_code` 用产品目录里的写法（科创板人工智能是 `950180.CSI`）。
- `available_at` 不晚于发布时刻。
- `family_id` 写产业方向，`canonical_branch_id` 写板块在该方向里的分支。同一指数下同一 `family_id` 的板块会聚合。

## 发布

生产镜像不带 `tools/`，容器的设置来自进程环境。先在仓库里生成容器内脚本，它原样内嵌 `tools/publish_etf_taxonomy.py`：

```bash
python -B -m tools.etf_taxonomy_container_script --taxonomy-file /absolute/reviewed-taxonomy.json > publish.py
ssh root@<生产主机> 'docker exec -i -e TAXONOMY_MODE=DRY stock-v4-worker python -B -' < publish.py
ssh root@<生产主机> 'docker exec -i -e TAXONOMY_MODE=PUBLISH stock-v4-worker python -B -' < publish.py
```

- `DRY` 走与发布相同的校验但不写入，返回 `DRY_OK`、将生成的映射版本、父版本、原行数和新行数。凡发布会拒绝的，`DRY` 同样报错。核对无误再执行 `PUBLISH`。
- 内容与当前已发布的完全相同时，两种模式都返回 `CURRENT`，不写入。
- `PUBLISH` 追加一个新的映射版本，返回 `PUBLISHED` 和新映射版本号。
- 避开五轮研判（09:35、10:35、13:05、14:05、14:30）和两个维护槽（10:00、14:15）。轮次状态为 `PUBLISHED` 后再发布。

测试环境直接运行 `tools.publish_etf_taxonomy`，加 `--dry-run` 只校验，命令见 [ETF V3.2 调测说明](etf-v3.2.md)。

## 核对与回退

```sql
SELECT version, available_at, body->'etf_taxonomy'->>'version',
       jsonb_array_length(body->'etf_taxonomy'->'rows')
FROM stock_v4_stage2.maintained_mappings
WHERE ns = 'stock-v4:prod:baseline' ORDER BY available_at DESC, version DESC LIMIT 2;
```

行数应等于归属文件的行数，产品、板块、权重数量与上一版相同。下一轮报告的 `input_meta.etf_policy.taxonomy_version` 应为新版本。

映射只追加不修改。回退时把上一份内容换一个新的 `version` 重新发布。

## 登记之后

登记只解除入场约束，配对真正可选还需要两样资料，都由维护任务推进：

- 指数成份权重。活跃板块已登记的指数与显式关联同级优先取，每次最多 8 个。
- 产品官方材料核验，见 [产品资料核验](etf-product-verification.md)。

## 当前登记（2026-11-20，`etf-taxonomy-v3.2-r3-dividend`，19 行）

| 板块 | 指数 |
|---|---|
| 芯片 `801001` | `H30184.CSI`、`H30007.CSI`、`931865.CSI`、`932066.CSI`、`000685.SH` |
| 算力 `801807` | `930851.CSI`、`931469.CSI`、`930713.CSI`、`931071.CSI`、`932456.CSI`、`950180.CSI` |
| AI应用 `803023` | `930713.CSI`、`931071.CSI` |
| 锂电池 `801004` | `931719.CSI` |
| 智能电网 `801346` | `931994.CSI` |
| 高股息精选 `801862` | `000922.CSI`、`000015.SH`、`H30366.CSI`、`932305.CSI` |

高股息是策略主题而非产业方向，由用户 2026-11-20 明确同意登记。本表随每次发布更新。
