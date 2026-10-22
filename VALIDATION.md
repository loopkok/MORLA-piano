# 验证记录

> 历史基线：以下内容保留初始独立引擎批次的验证结果，其中“没有改变MORLA”仅适用于该次批次，不能描述后续Web集成后的现状。最新分批证据与真机验收边界见 [2026-10-20—22调试日志](docs/DEBUG_LOG_2026-10-20_22.md) 和 [Changelog](CHANGELOG.md)。本次整理未重跑全量测试。

日期：2026-10-21（北京时间）。

本地 `/Users/a1234/Desktop/MORLA-piano` 与远端 `loopkok@192.168.0.231:/home/loopkok/MORLA-piano` 均通过17项 unittest。覆盖确认后的左右手指序、拇指保持、单键互斥、换曲、跨手抬指间隔、换位间隔不足、显式指法/姿态、休止/八度/物理键号、黑键显式标定、错误参数、重播约束、正常停止、反馈故障、迟到停止、到位超时、空闲控制冲突及未取得控制权时不急停他人机器人。

远端使用已部署的 ROS 2 Humble 与 MORLA 安装环境，`ROS_DOMAIN_ID=73`、`ROS_LOCALHOST_ONLY=1`。驱动组合为 MORLA Nero fake 驱动、MORLA XHand 适配器、本项目 native XHand 软件后端；没有启动硬件 SDK、CAN、串口或实体动作。

最近一次完整联调：

- `scores/demo.json`：12个音、4次手腕移位，状态 `FINISHED`，无 fault；计划动作与软件实际调度的最大迟到13.765毫秒。
- `scores/template.csv`：10个音、2次手腕移位，状态 `FINISHED`，无 fault；最大迟到13.994毫秒。
- 两次演奏结束均释放手指，左右手移位回到0。
- 联调结束后检查无本次测试遗留的驱动、适配器或测试后端进程。

原始证据保存在本地与远端的 `logs/ros-performance.jsonl`、`logs/ros-csv-performance.jsonl`、`logs/ros-inspect.json`，远端还保留 `logs/ros-fake-drivers.log`。日志目录不纳入源码版本管理。

运行 `bash scripts/ros_smoke.sh` 可重新执行完整 ROS 联调；需先加载 ROS/MORLA 环境并设置测试域。运行 `python3 -m unittest discover -s tests -v` 可重跑17项纯软件测试。

软件后端即时跟随目标，不模拟惯性、琴键接触或发声。上述延迟属于该次软件进程调度观测，不是硬实时保证，也不是实体落键/声音延迟。初始位姿、手指角度、换位关节量、移动净空与按键力度仍需现场标定。没有改变现有 MORLA 源码、构建产物或运行配置。
