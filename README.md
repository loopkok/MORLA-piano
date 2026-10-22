# MORLA Piano

> 2026-10-22 更新索引：[Changelog](CHANGELOG.md)、[调试日志](docs/DEBUG_LOG_2026-10-20_22.md)。下文包含初建模板与CLI流程；当前Web部署基准为左C3、右C5，实际标定保存在服务器 `/home/loopkok/MORLA/.morla_piano/config.json`。模板的占位值和右C4示例不能覆盖现场配置。首次Web准备走四段工作位轨迹，再次演奏直接回演奏零位；最新操作差异以日志为准。

独立的 Nero 双臂 + XHand 双手钢琴演奏项目。复用 MORLA 安装好的驱动和消息接口，不把演奏逻辑写入 MORLA，不需要遥操、Quest、手部重定向或策略推理。

当前交付的是可运行、可标定的流程框架。初始关节、手指按键关节、每白键的机械臂增量均为**占位值**，没有完成实体琴键标定和声音验收。硬件运动要求配置 `calibrated: true`；纯 Python fake 和 ROS fake 可直接验证。

## 已确认的键位和指序

61键暂按 C2—C7，最左键编号为1；每区包含7个白键、5个黑键。实际键盘若不同，修改 `keyboard.first_note`。

- 左二区从左第13个物理键 C3 开始：小指 C3、无名指 D3、中指 E3、食指 F3。
- 左三区从左第25个物理键 C4 开始：右手食指 C4、中指 D4、无名指 E4、小指 F4。
- 两手均不用拇指。所有下发消息保留进入准备动作时实测的三个拇指关节值。
- 全局一次只按一个键；左右手可交替，暂不接受和弦或重叠音。

## 本地零依赖验证

Python 3.10+，在本项目目录运行，无需 ROS 或 MORLA：

```bash
python3 -m morla_piano plan scores/demo.json --out /tmp/piano-plan.json
python3 -m morla_piano run scores/demo.json --fast
python3 -m morla_piano run scores/template.csv --fast
python3 -m unittest discover -s tests -v
```

去掉 `--fast` 就按真实时间运行软件后端。`--fast` 使用虚拟时间，不是物理仿真。日志在 `logs/performance.jsonl`，记录计划/实际时刻、手指、音符、移位和错误。

## 远端 ROS 软件联调

项目部署位置为 `/home/loopkok/MORLA-piano`，MORLA 在 `/home/loopkok/MORLA`。登录使用 SSH，密码不保存在本项目。

```bash
cd ~/MORLA-piano
source /opt/ros/humble/setup.bash
source ~/MORLA/install/setup.bash
export ROS_DOMAIN_ID=73
export ROS_LOCALHOST_ONLY=1
bash scripts/ros_smoke.sh
```

脚本在独立 ROS 域启动 MORLA 的 Nero fake 驱动、真实 XHand 适配器和本项目的 native XHand 软件后端，然后执行 JSON/CSV 两份谱表。退出时只终止该脚本启动的 launch。不开 CAN、串口或真实电机。

也可以在一个终端保持测试驱动：

```bash
ros2 launch ./launch/piano_drivers.launch.py mode:=fake
```

另一个终端使用相同 ROS 环境与域：

```bash
python3 -m morla_piano session --backend ros --expect-driver fake
```

## 实际使用流程

1. 先把机器人移动到方便、安全的操作起点。结束原有遥操/策略等命令写入进程，避免同一话题存在多个 writer。
2. 用外部 launch 启动所需 MORLA 驱动。该 launch 不启动 Quest、遥操、重定向、策略或协调器。硬件 CAN 的初始化、急停复位由现场已验证流程处理；本项目不自动冷启动或复位。
3. 启动演奏会话。`prepare` 会获取演奏控制权，显式请求双臂流式使能，先抬四指，再从实测当前关节低速插值至配置的钢琴初始位，等待双臂实测到位。
4. `play <谱表>` 按谱演奏；或 `start <谱表>` 一次完成准备、到位和演奏。
5. 演奏中输入 `stop` 会保持当前实测机械臂位置、抬指并停止。每次重播先 `prepare`，也可直接再次 `start`。

硬件启动示例（串口和 CAN 名按现场填写）：

```bash
ros2 launch ./launch/piano_drivers.launch.py mode:=hardware \
  config:=/home/loopkok/MORLA-piano/config/piano.calibrated.json \
  left_can:=can_nero_left right_can:=can_nero_right \
  left_serial_port:=/dev/serial/by-id/LEFT_DEVICE \
  right_serial_port:=/dev/serial/by-id/RIGHT_DEVICE
```

在同域另一个终端启动会话：

```bash
cd ~/MORLA-piano
python3 -m morla_piano --config config/piano.calibrated.json session --backend ros
```

会话命令：

```text
status
prepare
play scores/demo.json
start scores/template.csv
position config/position.example.json
capture /tmp/piano-captured.json
stop
quit
```

`position <文件>` 接收左右各7维原生关节角（rad），立刻将此位置设为本次会话的钢琴初始位并移动到位；使用前先填写实测值。文件格式见 `config/position.example.json`。它仅更新本次会话，按键间距和移位标定也必须与新初始位置匹配。`capture` 另存测量值，不会自动把配置标为已标定。

`inspect`、`capture` 也可在未取得控制权时读取，不创建运动或控制权 publisher：

```bash
python3 -m morla_piano inspect --backend ros
python3 -m morla_piano capture --backend ros --out config/piano.captured.json
```

若原有系统仍在发布 joint_commands 或控制权，本项目会拒绝接管；停止原 writer 后再连接。不要同时启动原全栈和本项目的相同驱动。

## 简谱时序表

JSON 的 `start`、`duration` 默认以拍为单位，依据 `tempo_bpm` 转换为绝对秒；也可设 `time_unit: "seconds"`。`start` 是从乐曲开头数的绝对起始拍，不是相对上一行的间隔。`duration` 是谱面时值；`gate` 是实际按住占比，剩余时间供抬指/换位。

```json
{
  "title": "我的曲谱",
  "tempo_bpm": 60,
  "tonic": "C4",
  "gate": 0.7,
  "events": [
    {"start": 0, "duration": 1, "hand": "left", "note": "1-"},
    {"start": 1, "duration": 1, "hand": "right", "note": "1", "finger": "index"},
    {"start": 2, "duration": 1, "hand": "right", "note": "0"},
    {"start": 4, "duration": 1, "hand": "right", "note": "5"}
  ]
}
```

- 简谱 `1`—`7` 按 `tonic` 解释，`1-` 低一个八度，`1+` 高一个八度，`#4`/`b7` 表示升降音。
- `C3`、`D4` 等绝对音名不受 `tonic` 影响；`key:13` 表示从琴最左端1起算的第13个物理键；`midi:60` 表示 MIDI 音高60。
- `0`、`-`、`rest` 是休止符；持续时间仍计入乐曲长度。
- `finger` 可选：`index`、`middle`、`ring`、`pinky`。未填写则优先选择当前手腕位置能覆盖的手指。
- `shift_white` 可选，指定相对初始位的白键步数；须与音符、指法或显式键位标定一致。
- CSV 使用相同列，见 `scores/template.csv`；速度、调号从配置 `score_defaults` 读取。
- 图片谱、PDF谱、MIDI文件不直接解析。先把谱图整理为 CSV/JSON，含准确时序和左右手；生成动作后可离线检查。未提供视觉识谱或自动指法优化能力。

## 标定参数怎么填

复制 `config/piano.placeholder.json` 为 `config/piano.calibrated.json`。所有角度均为**原生关节 rad**，不是度，不重复左右镜像；手臂顺序是本侧 J1—J7，手的关节顺序与 MORLA XHand 一致。

1. `hands.<side>.initial_arm_rad`：食指/小指对准约定键位时的7维双臂实测关节。可先用 `capture` 另存当前位置。
2. `hover_rad`：手位于键面上方的12维关节姿态。拇指部分不会被演奏动作覆盖，实际始终保留实测拇指。
3. `press_rad.<finger>`：只填该手指需要改变的具名关节及按下目标。抬起恢复 `hover_rad` 中该手指的值。允许每指只驱动一个关节，禁止写入其他手指或拇指关节。
4. `arm_step_rad_per_white`：手掌向高音移动一个白键的7维关节增量。向低音用负步数。此线性近似只适用于已测范围，`shift_range_white` 限制范围。
5. 更精确时，用 `arm_positions_rad` 按移位步数填写绝对7维姿态，如 `"1": [7个数]`、`"-1": [7个数]`。规划优先使用这些实测点，否则用初始位+增量。当前实现采用关节空间；若测的是末端位移，先通过已验收 IK 换成7维关节姿态，不向未确认坐标系直接发 Pose。
6. 黑键需要前后方向位移，不从白键间距猜测。填写 `key_overrides`，如 `"F#4": {"finger": "index", "shift_white": 5}`，并为 `arm_positions_rad["5"]` 填写该键的实测姿态。此处5是姿态/位移索引，显式 override 时可代表专用黑键姿态。未标定黑键会拒绝。
7. 根据测试调整 `move_duration_s`、`settle_s`、`release_settle_s`、`prepare_duration_s` 和速度/误差上限。原 MORLA 驱动的限速继续生效，若实际到位跟不上谱，会停止；应调慢速度/增加休止，不能把“命令已发送”当成“已到位”。
8. 完成上述实际测试，再设置 `calibrated: true`。不要直接把占位参数标为已标定。

动作时序以单调时钟为基准，先生成全曲计划，再取得控制权。每两个音之间必须留出 `release_settle_s` 抬指时间，左右手交替也一样。需要移位时，先保证上一次本手按键已释放并留出恢复时间，再插值换位；只有手臂实测误差满足阈值并持续指定时间才允许下一次按键。换位时间不足会预先报具体行号。当前为局部优先指法规划；复杂曲目可手工指定指法/移位或增加休止。

## 状态和边界

`CONNECTED → PREPARING → READY → PLAYING → FINISHED`；停止进入 `STOPPED`，反馈失效、跟踪误差、拒绝命令或时序超限进入 `FAULT`。

正常停止保持当前位置并抬四指。反馈或控制权故障时停止发运动目标，向两臂请求电子急停并释放控制权；这不等价于已实测停稳，也不能保证断连后手指能抬起，现场急停仍用于硬件故障处理。故障后重新建立会话并按现场流程恢复。

目前未实现琴键接触检测、音频识别、碰撞规划、自动避障路径或硬实时控制。起点到初始位及各换位使用关节插值，须由现场标定验证路径空间与琴面净空。软件验证确认消息/顺序/时序流程，不证明实体按键力度或声音正确。

代码入口：`morla_piano/model.py` 解析与规划，`engine.py` 状态机与调度，`ros_backend.py` ROS 接口与控制权，`cli.py` 命令行；`launch/piano_drivers.launch.py` 仅复用 MORLA 驱动，`scripts/ros_smoke.sh` 执行隔离软件联调。
