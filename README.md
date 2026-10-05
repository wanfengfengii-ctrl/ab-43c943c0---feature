# MIDI Timeline Normalizer

数字音乐档案馆的时间轴归一化服务：把标准 MIDI 文件（SMF）中的通道事件换算到统一
微秒时间轴，多轨合并与速度变化不会引起演奏时刻漂移。全程仅使用 Python 标准库，
Docker 构建无需安装任何依赖。

## API

### `POST /api/midi/normalize`

请求体为原始 MIDI 字节（不超过 1 MiB）。支持格式 0、1 与正数 PPQN；
轨道数与通道事件数之和不得超过 10000。

**成功（200）**：按 `(tick, track, order)` 稳定排序的通道事件，每个事件带精确
微秒时刻（最简分数）：

```json
{
  "format": 1,
  "ppqn": 480,
  "track_count": 3,
  "channel_event_count": 8,
  "events": [
    {
      "tick": 1,
      "track": 1,
      "order": 1,
      "type": "note_off",
      "channel": 0,
      "data": [60, 0],
      "time_us": {"numerator": 3125, "denominator": 3, "fraction": "3125/3"}
    }
  ]
}
```

**可听音符投影**：追加查询参数 `?projection=audible_notes` 后，成功响应在保留
`events` 的同时按起音顺序增加 `notes`，逐项区分按键释放与真正结束发声：

```json
{
  "channel": 0,
  "pitch": 60,
  "velocity": 100,
  "start_tick": 0,
  "release_tick": 240,
  "end_tick": 960,
  "start_us":   {"numerator": 0,      "denominator": 1, "fraction": "0/1"},
  "release_us": {"numerator": 250000, "denominator": 1, "fraction": "250000/1"},
  "end_us":     {"numerator": 750000, "denominator": 1, "fraction": "750000/1"}
}
```

- 正力度 `note_on` 起音；`note_off` 或零力度 `note_on` 释放同通道同音高按键。
- CC64（延音踏板）值 ≥ 64 期间，释放只记录 `release_tick`，声音继续延音；
  CC64 首次降到 64 以下时，该通道全部待延音音符在同一 tick 结束（`end_tick`）。
  踏板状态按通道独立，初始为抬起；无踏板时释放即结束。
- 延音中允许同音高再次起音，两次起音各自独立；同刻事件沿用
  `(tick, track, order)` 次序。
- 投影冲突返回 **422** 且无部分投影（响应不含 `notes`）：未释放即重复起音
  （`note_on_without_off`）、无对应按键的释放（`note_release_without_on`）、
  文件结束仍有未结束音符（`note_unclosed`，含踏板仍踩住的延音）。错误体在
  `location` 中给出 `tick`/`track`/`order` 定位：

```json
{"error": {"code": "note_on_without_off", "message": "...", "offset": null,
           "location": {"tick": 100, "track": 0, "order": 1}}}
```

省略 `projection` 时响应、排序与错误语义完全不变；未知的 projection 值返回
400 `invalid_projection`。

**结构错误（400）**：返回可定位的字节偏移，绝不返回部分时间轴：

```json
{"error": {"code": "truncated_track", "message": "...", "offset": 18}}
```

其他状态码：413（超过 1 MiB）、405 / 404 / 411。

### 语义要点

- 初始速度 500000 µs/四分音符；格式 1 仅以首轨速度事件建立全局节拍表，
  速度在其所在 tick 起生效（同 tick 多个速度事件取最后一个）。
- 严格校验：文件块、变长整数（≤4 字节）、运行状态（meta/sysex 会取消）、
  事件长度、数据字节高位；拒绝截断数据、非法状态与尾部未声明字节。
- 时间以 `Fraction` 精确累计，输出为最简微秒分数。

### `GET /health`

返回 `{"status": "ok"}`，供 Compose 健康检查使用。

## 运行

```bash
docker compose up --build app            # 默认宿主机端口 8000
HOST_PORT=9000 docker compose up app     # 可配置宿主机端口
```

## 验证（一次性 verify 服务）

verify 服务在 app 健康检查通过后依次执行：单元测试 → 构建检查
（compileall + 模块导入）→ 含变速多轨文件的 API 冒烟（含错误用例，以及
跨轨同刻、变速、延音与 422 失败回归的 audible_notes 投影），
并以退出码报告结论（0 = 通过）：

```bash
docker compose up --build --exit-code-from verify verify
echo $?   # 0 表示全部通过
docker compose down
```

## 本地开发（无 Docker）

```bash
python3 -m unittest discover -s tests -t . -v   # 单元测试
PORT=8000 python3 -m app.server                 # 启动服务
APP_URL=http://127.0.0.1:8000 python3 -m verify.verify
```

## 结构

```
app/midi.py      严格 SMF 解析、节拍表、精确分数时间、audible_notes 投影
app/server.py    HTTP 前端（stdlib http.server）
tests/           单元测试（63 例，含延音投影语义）
verify/          一次性验证服务（测试 + 构建检查 + API 冒烟）
Dockerfile       python:3.12-slim，无依赖安装
docker-compose.yml  app（健康检查、可配置宿主机端口）+ verify
```
