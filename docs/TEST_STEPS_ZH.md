# 工程工作台测试步骤

## 交付约定

测试文件保留在项目内。本说明及入口脚本只在你手动运行时执行测试；开发助手不再运行测试、不安装或启动 Docker、不调用真实模型、不推送远程仓库、不操作生产系统。

## 1. 前置条件

- Python 3.11+、Git、Node.js。
- 仅测试本机和模拟适配器时不需要模型 Key 或 Docker。
- 测试使用可销毁的临时目录与本机 Git 库，不选择真实业务工程。
- 模型测试中的 HTTP、执行器测试中的 Docker 传输使用测试替身。它们通过不代表真实网络模型或容器已验证。

## 2. 安装与运行

在项目根目录执行：

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite All
```

若脚本执行策略受企业策略限制，请不要绕过企业控制，改用下面的 Python 命令。

```powershell
python -B -m pytest tests/test_local_workspace.py tests/test_local_workspace_api.py tests/test_engineering_ai.py tests/test_engineering_runs.py tests/test_engineering_run_api.py tests/test_execution_sandbox.py tests/test_execution_api.py tests/test_engineering_operations.py tests/test_operations_api.py tests/test_operations_api_provider_state.py tests/test_production_adapters.py tests/test_production_api.py tests/test_production_uncertainty.py -q -rs -p no:cacheprovider --basetemp=.lfwork/manual-engineering-001
```

每次使用新的 `--basetemp` 名称。pytest 会删除重建该临时目录，不可填写用户数据或真实工程路径。

分组入口：

```powershell
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite Core
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite AI
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite Runs
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite Sandbox
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite Operations
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite Production

回归入口也包含在 `Operations` 分组；每次使用新的 `--basetemp` 名称。实现助手不会自动运行这些命令。
```

## 3. 检查结果

- `passed`：该测试断言通过。
- `failed` / `errors`：不得宣称功能通过。保留错误输出后修复。
- `skipped`：前置条件不足或未开启真实集成，不能算真实能力通过。
- Unit/mock：只证明约束、流程和协议处理。
- Real/local：只证明可销毁目录、本机 Git 或真实 HTTP 服务路径。
- Real/external：需单独配置，默认不运行。

前端检查：

```powershell
node tools/check_web.mjs longflow/web
node tests/test_safety_web.mjs
node tests/test_files_ai_web.mjs
node tests/test_operations_web.mjs

生产状态只读检查：打开 Git/恢复操作区域，确认生产能力只展示 `not_connected`，且没有生产发布、数据库迁移或数据库恢复执行按钮。只有在服务端显式注入可信 `ProductionAdapters` 后，才可对部署、迁移或恢复生成提案；提案执行必须提供准确的审批 ID、提案 ID 和 `confirmed:true`，并验证提供者证据。默认测试不注入生产适配器。
```

## 4. 浏览器文件工作区

重启服务并打开 `http://127.0.0.1:8765/#files`。选择玩具目录，读取、增删改、生成差异，确认原目录不变；明确批准后验证原文件和新 Git 版本。比较两个版本，查看旧文件，生成恢复预览，批准前确认原目录未改变。详细流程见 LOCAL_WORKSPACE.md。

## 5. AI 工作区

只有配置真实 OpenAI 兼容模型后才可验证真实理解、规划和编码。勾选发送代码上下文的明确同意，再分析/规划；选定确切可修改文件后生成 AI 候选。检查越界、模型失败、源文件变化和冻结后是否拒绝，不应自动写原目录。配置为 local 时必须显示未接入，不能假装模型结果。

## 6. 长程任务

在文件审阅 AI 区填写目标并同意发送代码，点击创建长程计划。读取计划中每个节点的目标、验收条件和依赖，勾选所有计划涉及的确切文件路径，确认计划。逐次点击推进：分析、候选生成、验证分别保存检查点。暂停/恢复/停止需核对持久状态；刷新页面后读取同一任务而非重新创建。未接入执行器时验证阶段应 blocked；这不是后台自动长程执行已完成。

持久服务测试覆盖计划确认、分阶段检查点、暂停/停止、迟到结果拒绝、预算限制和重启后 in_doubt 隔离。未接入执行器时必须 blocked，不能自动完成。当前逐阶段推进与后台自动持续执行不是同一能力，按能力状态核验。

## 7. 执行沙箱

默认禁用。真实测试需你安装 Docker，使用审核过且以 sha256 digest 固定的测试镜像，镜像包含所需 pytest/ruff。不得退回宿主机执行。验证只读候选挂载、无网络、非 root、资源限制、超时停止和清理。具体 opt-in 环境变量以 test_execution_sandbox.py 的真实集成测试说明为准。未配置只应验证 not_connected/blocked。

工作台流程：载入节点候选或生成手工候选，审阅差异和候选哈希；选择 pytest、compileall 或 ruff，勾选当前候选的独立执行授权，再点击批准并执行候选。读取结果及执行证据，核对状态、退出码、候选版本和清理结果。预设变化或候选失效后应清除执行同意。必须获得任务要求的全部验证证据后再手动推进验证；之后审阅差异、批准应用原目录，最后点击记录已应用，由服务端核验真实版本回执。审批执行不等于审批修改原目录。应用后、记录前刷新或关闭页面，再打开同一工作区，应恢复“待记录应用”的提示；本地存储仅供展示恢复状态，不能代替服务端回执验证。过期的执行证据不能推进任务。

## 8. Git、CI/CD 与数据库

本机测试覆盖分支和受限快进操作；远程推送仅模拟传输，不推真实远程。Git 与恢复 API 先返回不可变提案，执行要求 `confirmed:true` 和单次审批。Git 还要求可信原生仓库精确匹配所选目录，推送另需 `confirm_push:true`；本地事务恢复不依赖原生 Git 仓库配置。CI/CD、生产部署、数据库迁移/恢复目前没有选定实际提供者；虽然存在严格的提案/审批 API，测试只验证默认关闭和授权边界。不能把拒绝测试通过视作真实发布、迁移或恢复能力完成。

页面 Git 流程：在 Git/恢复区域读取能力，选择分支、受限快进合并或推送，填写对应字段并生成提案。核对预览摘要、引用、目标及有效期，勾选明确确认；推送另勾选推送确认，再执行。字段或操作类型变化后必须重新预览和确认。需要恢复时只能先读取恢复提案、审阅并确认，安全冻结期间不可恢复。所有结果必须区分 configured、not_connected、completed 和失败，不把未配置状态当执行成功。

## 9. 崩溃与恢复

在测试替身中注入写入中断，确认持久日志、部分状态、源漂移拒绝与恢复预览绑定。恢复必须明确批准，不自动重写原始文件。多文件应用不是原子事务；实际事故保留目录和事务日志，不自行删掉日志来解除锁定。
