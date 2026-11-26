# AI 助手表情包目录

选图脚本默认读取项目根目录的 `sticker-index.jsonl`。每行对应一张图片，含仓库相对路径。只有已完成有效标注的记录可用于选图。运行时不调用视觉模型。

## 生成目录

安装 `requirements-site.txt` 中的 Pillow 后，可以初始化项目根目录的索引：

```powershell
python -X utf8 scripts/sticker_index.py
```

索引复用已完成的视觉标注。新增素材按批次检查可见文字、原图和动图关键帧，校验完字段后再写入索引。`id` 和 `path` 对应原图，使用 `sha256` 确认记录与素材内容一致。`pending` 代表等待分析，`error` 代表本次分析没有得到可用记录。

批量分析使用已登录的 Codex CLI 连接云端视觉模型。以下命令从已有索引续跑，只提交待分析项及要求重试的失败项。字节完全一致的图片保留各自路径并复用标注：

```powershell
python -X utf8 scripts/analyze_stickers_cloud.py --workers 3 --batch-size 2 --timeout 100 --retry-errors --wait-for-limit
```

`--wait-for-limit` 让后台进程在临时限额期间暂停提交。服务返回可识别的重试时间时按该时间等待，否则延长重试间隔。等待结束先提交一张图，校验通过后恢复原有并行数。进程须保持运行。登录或账户计费异常会停止，已完成的索引可用于下次续跑。

索引会在每批通过校验后自动更新。后台进度保存在 `.sticker-catalog/cloud/full.log`，停止原因保存在 `.sticker-catalog/cloud/full-errors.log`。`python -X utf8 scripts/sticker_index.py --verify` 可在后台运行时校验索引快照并报告是否完成。批处理停止后才能手工导入标注或重建清单。

## 目录记录

每条记录包含 `id`、仓库相对 `path`、`series`、`sha256`、`media`、`analysis_status`，以及分析完成后的 `analysis` 和 `evidence`。主要分析字段包括：

- `visible_text`、`visual_summary`、`motion_summary`、字面含义、实际含义、图文关系及无障碍描述。
- 主次意图、情绪、强度、倾向、表达对象、态度、可能被理解的任务承诺。
- 正式度、温暖感、亲密度、表现张力、调侃力度、幽默类型、抽象程度与类型、梗门槛、接话依赖、玩笑明显度。
- 适用与避免场景、示例与反例、风险标签、误读说明、发送方式、语言、圈层、来源和检索词。

`machine_labeled` 表示通过结构校验的模型结果，`visually_reviewed` 表示额外做过目视复核。`pending` 和 `error` 不参与选图。`evidence.frames` 使用从零开始的帧序号。动图证据是抽样画面，短暂闪过的动作或文字可能漏掉。

### 经过迭代验证的选图字段

| 字段 | 标注和匹配方式 |
| --- | --- |
| `selection_profile.occasion` | `generic`、`morning`、`bedtime`、`spring_festival`、`meal`、`other`。有明确节庆或时间含义才标专项场合。普通回复会排除带其他专项场合的图。 |
| `selection_profile.action_scope` | `generic`、`purchase`、`attend`、`scout`、`other`。标图里明确作出的承诺，尤其要区分「去买」「一定来」「我去探路」。`generic` 和 `other` 无法满足一项已知具体动作的严格匹配。 |
| `selection_profile.response_mode` | `generic`、`explicit_ack`、`implicit_ack`、`empathize`、`cheer_up`、`reassure`。「收到」才可标明确收悉，「好的」通常只是隐含确认。承认对方感受、劝开心、给出安心保证分别标注。 |
| `analysis.expressiveness` | `0–4` 的表现张力。Persona 可以传软偏好。确实要求克制时，可以另设最大值。 |

保留原来的意图、承诺状态、表达对象、受众限制、风险、抽象程度和单选 `send_mode`。填表先看**助手把图发出去会表达什么**，尤其留意文字反转、图中人称和动图跨帧变化。分析不确定时写明依据和置信度。

## 运行时选图

助手先确定本轮需要表达的意图、受众、当前任务状态和放图位置。Persona 提供抽象范围、幽默偏好、梗门槛、温暖感及表现张力等参数。例如：

```powershell
python -X utf8 scripts/select_sticker.py --intent understand --audience team_private --task-state accept --placement after_text --abstraction 1:3 --humor 荒诞 --context 刚理清口径 --text '终于搞懂分母了' --limit 3
```

明确收悉和确认赴约的例子：

```powershell
python -X utf8 scripts/select_sticker.py --intent acknowledge --audience team_private --task-state received --response-mode explicit_ack --require-response-mode --limit 1
python -X utf8 scripts/select_sticker.py --intent accept_task --audience friend --task-state accept --action-scope attend --require-action-scope --max-risk 1 --limit 1
```

标准输出是 JSON 数组，候选项含相对路径、匹配理由和分析结果。`--limit 1` 只取首选。用返回的 `path` 定位原图，按聊天应用的附件机制发送。空数组表示本轮只回文字。严肃场景可传 `--serious`，有明确张力上限时传 `--max-expressiveness`。通过 `--catalog` 可指定另一份目录。旧目录缺少新字段时仍能使用普通筛选，明确要求精确匹配时会跳过缺少所需字段的图片。
