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
（compileall + 模块导入）→ 含变速多轨文件的 API 冒烟（含错误用例），
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
app/midi.py      严格 SMF 解析、节拍表、精确分数时间
app/server.py    HTTP 前端（stdlib http.server）
tests/           单元测试（41 例）
verify/          一次性验证服务（测试 + 构建检查 + API 冒烟）
Dockerfile       python:3.12-slim，无依赖安装
docker-compose.yml  app（健康检查、可配置宿主机端口）+ verify
```
