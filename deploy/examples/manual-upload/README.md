# 手动上传评测样本

适用 target：guardrail-privacy（NeMo Guardrail / Privacy & prompt injection）。
适用 dataset：ds-guardrail v1（Guardrail conversation safety）。

CSV 和 XLSX 内容一致，任选一个上传：12 条虚构样本，6 条安全、6 条风险，包含中英文及一条多轮对话。
风险覆盖第三方隐私访问、批量越权导出、密钥索取、指令覆盖、角色伪装、多轮越权升级。

字段：messages 是 JSON 对话数组（input）；has_issue 是人工预期的 true/false（reference）；category 是场景分类（metadata）。
预期标签是示例假设，需按实际 Guardrail 政策复核，不代表已测性能。没有真实用户数据或真实凭据。

使用：Evaluation → 当前 target 的 Config → Data source 选择 Upload for this evaluation → Save config；
然后 Trigger → 上传其中一个文件 → 校验通过后运行。名称可留空。
只准备了文件，未修改 Config、未上传服务器、未触发评测。两个文件已用当前后端同一解析器校验，结果一致。
当前 mock-score 固定返回 1，不能用它衡量这些样本的实际识别准确率。
