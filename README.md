# PikPak Fastlink PyFork

PikPak 自用秒传工具：浏览器登录后复制短期凭据，Python 读取项目目录的 `.env`，执行本地计算、保留目录结构的秒传导入、云端递归导出、目录比对、批量处理与断点续跑。

网络请求使用 `curl_cffi` 的 Chrome impersonation。工具采用严格秒传：服务器确认文件内容已完成才记为成功；未命中时记录 `not_reusable`，不发送文件正文。它不自动登录或刷新 token，也不执行文件删除、覆盖、移动或 OSS 普通上传。

## 安装与入口

需要 Python 3.11 或更高版本。在本目录执行：

```bash
python -m pip install -e .
python pikpak.py --help
```

也可以使用 `python -m pikpak_fastlink` 或安装后的 `pikpak-fastlink` 命令。下面统一使用 `python pikpak.py`；参数相同。

## 浏览器凭据 → .env

1. 在 Tampermonkey 中安装 `userscripts/pikpak-credentials.user.js`。
2. 重新加载 `https://mypikpak.com/` 并登录，打开一个目录以触发 API 请求。
3. 点击右下角“复制 PikPak .env 凭据”。
4. 将剪贴板内容粘贴到本项目根目录的 `.env`，与 `pikpak.py` 同目录。

格式如下，空值由真实捕获值替换：

```dotenv
PIKPAK_AUTHORIZATION=""
PIKPAK_DEVICE_ID=""
PIKPAK_CAPTCHA_TOKEN=""
```

`PIKPAK_AUTHORIZATION` 是完整的 `Bearer …` 请求头值。脚本从官方请求捕获当前 Authorization 和设备 ID；点击按钮时，调用已登录页面的 SDK 获取一次新的 `POST:/drive/v1/files` captcha token，再复制三项为 `.env` 文本。只读请求里的 token 不一定适用于创建文件，不能直接当成永久通用 token。

缺少完整凭据或页面 SDK 未就绪时，先刷新并登录，在网页中操作目录后再复制。复制期间登录或设备发生变化时不输出旧凭据；若网页 SDK 要求交互验证，由用户在网页完成。按钮不维护后台刷新，也不创建云盘文件。它只负责短期凭据复制，不提供秒传导入或导出 UI。

Python 默认读取包所在项目目录的 `.env`，与当前工作目录无关；同名进程环境变量优先。`.env` 为普通文本，不需要加密或密钥库。`.env`、运行状态和 E2E 目录已被 Git 忽略，仓库只提供空值 `.env.example`。不要把真实凭据提交到 Git。

凭据失效时命令停止，并保留可续跑的状态。重新通过浏览器复制凭据、更新 `.env`，再执行原命令即可。curl impersonation 不保证风控一定放行；本工具不自动处理验证挑战。

## 文件夹 ID 与 JSON 格式

远程参数使用 PikPak 的不透明字符串文件夹 ID；可从网页请求的 `parent_id` 获取。参数中的 `root` 表示云盘根目录。首次验证应使用专门的 `probe/` 文件夹及其子目录。

导入和导出使用同一种 JSON：

```json
{
  "files": [
    {
      "path": "/Season 01/episode.mkv",
      "gcid": "0000000000000000000000000000000000000000",
      "size": "422649548"
    }
  ]
}
```

示例 GCID 只是格式占位值，实际文件应由计算器或云端导出取得。必需字段为 `path`、40 位十六进制 `gcid`、非负字节大小 `size`。远程 API 的 `hash` 映射为 JSON 的 `gcid`；输出 GCID 统一为大写。云端导出还可带 `fileId` 和 `parentId`，导入不依赖它们。

路径相对于指定的远程目标目录；导入会建立文件路径需要的子目录。`..`、反斜杠和歧义路径被拒绝。同名冲突不覆盖、不删除。清单只描述文件及其父路径，不单独保存空目录。

## 本地计算与保留结构上传

```bash
python pikpak.py generate-json --source-dir Movies --output-file movies.json --workers 8
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --dry-run
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --workers 4
```

例如本地 `Movies/Season 01/episode.mkv` 对应目标目录中的 `Season 01/episode.mkv`。本地源目录的名称不会自动加到清单路径前面；想保留这个外层名称，可先在云盘建立同名目标目录。

本地计算不需要凭据。独立计算器也可在未安装第三方依赖时运行：

```bash
python pikpak_fastlink/local_calculator.py --source-dir Movies --output-file movies.json --workers 8
```

GCID 是按文件顺序拼接各块 SHA-1 二进制摘要后，再做一次 SHA-1。块大小匹配 PikPak 网页 WASM：用文件大小的低 32 位选择 256 KiB 至 2 MiB 的块，实际读取、哈希和输出大小仍覆盖完整文件。已对照网页 WASM 的空文件、`abc` 和大小边界；大于 4 GiB 的完整文件哈希在服务器和其他客户端上的兼容性尚未验证。

计算期间检查文件大小、时间和已打开文件身份，读队列有界。检测到变化或输出失败时保留原输出。降低 `--workers` 可减少机械硬盘上的并发读取。

## 在线提取秒传 JSON

```bash
python pikpak.py export-json --source-parent-id <folder-id> --output-file remote.json --state-file .state/export.state.json --workers 4
```

递归读取文件夹和服务器分页 token，从文件的 `name/hash/size` 构建保留相对路径的清单，无需下载内容。输出是本工具可导入的 JSON，不是官方分享链接。

列表完整性检查覆盖分页 token、重复 ID/名称、父目录、文件名和已完成文件的哈希/大小。短页和空中间页不代表结束，只有空的 `next_page_token` 表示结束。歧义或不完整扫描不会发布部分输出。扫描源不是目录快照，运行时尽量保持源目录稳定。

中断后用相同参数重跑，从导出检查点继续；完成的状态对应已有结果。如需重新扫描变化后的远程目录，应使用新的状态文件。

## 重试与续跑

- 原命令使用相同清单、目标 ID 和状态路径重跑；已完成记录跳过。
- 状态绑定清单内容 SHA-256 和目标目录，输入变化或换目标时使用新状态文件。
- 429、服务端错误和传输失败进行有限重试；凭据失效停止继续调度。
- 创建目录或文件的响应丢失时，先重新查询目标目录，再决定是否重试，避免盲目重复创建。
- 失败或未命中的记录写入状态旁的 `.retry.export.json`。
- `--retry-failed` 重试失败和 `not_reusable` 记录；全部完成后移除过期的 retry 清单。
- import 命令遇到失败、凭据失效或秒传未命中时返回非零退出码。

```bash
python pikpak.py import-json --file movies.json --target-parent-id <folder-id> --state-file .state/movies.sqlite3 --retry-failed --max-retries 5 --flush-every 100
```

服务器在秒传未命中时可能先创建一个未完成的云盘条目。工具报告未命中，不自动删除它；该条目也不会被当成已完成文件。若它造成同名冲突，需在网页中自行确认处理后再重试。

状态按任务、目标和账号分别保存，不跨账号复用；工具没有账号管理功能。

## 批量导入与批量比对

```bash
python pikpak.py batch-import-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/import --json-parallelism 2 --workers 4 --dry-run
python pikpak.py batch-import-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/import --json-parallelism 2 --workers 4
python pikpak.py batch-check-json --input-dir manifests --target-parent-id <folder-id> --state-dir .state/check --output-dir deltas --with-checksum
```

批量命令递归发现输入目录的 JSON，为每个清单保存独立状态。所有清单的文件路径都相对于同一个远程目标；不会按 JSON 文件名额外建立一层目录。导入前检查跨清单路径冲突。输入、状态、差异输出目录必须互不重叠。

`batch-check-json` 默认按路径判断存在性，`--with-checksum` 同时检查 GCID 和大小；输出只包含未对齐记录的 `.delta.export.json`，可再用于导入。未完成远程文件不算对齐。已完成的比对状态复用原结果，需要重新比对远程变化时使用新状态目录。

## 单目录比对

只比较相对路径，输出本地缺失于远端的路径：

```bash
python pikpak.py compare-folder --local-folder Movies --remote-folder-id <folder-id>
```

加 `--remote-only` 可反向比较。目录模式不读取内容、不计算本地哈希。

按 GCID 比较已有清单和远程树，输出缺少的内容：

```bash
python pikpak.py compare-folder --local-json movies.json --remote-folder-id <folder-id> --output-file missing.json
```

JSON 模式以 GCID 判断内容存在性；远端重命名或平铺仍可匹配。同一内容的多个路径按内容存在处理。`--remote-only` 选择只在远端存在的内容。结果保留选定一侧的相对路径，并原子替换输出文件。

## 验证

```bash
python -m pip install -e '.[test]'
python -m pytest -q
node --test tests/test_userscript.cjs
```

Python 测试覆盖真实 PikPak 协议形状、环境加载、指纹请求选项、严格秒传判定、分页、目录冲突、重试、续跑、批量操作与原子输出；油猴测试覆盖请求观察和复制行为。测试仅使用合成凭据，真实账号验证限定在 `probe/` 内。

浏览器主世界注入验证可检查实际请求捕获与 UI，但不等同于安装 Tampermonkey 后的扩展沙箱验证。专门用于秒传提取/导入的油猴脚本不属于本项目当前实现范围。
