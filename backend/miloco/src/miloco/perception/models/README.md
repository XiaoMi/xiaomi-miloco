# 感知 ONNX 模型

这个目录**不进 git**（`.gitignore` 里只放行本 README）。5 个模型合计约 78MB，放在
git 历史里意味着：每次 CI `actions/checkout`（默认浅克隆）都要白付一遍流量，且以后
每换一次模型就在历史里永久叠一份。

模型托管在固定 tag `models` 的 GitHub Release，清单（文件名 / 大小 / sha256 / 是否必需）
锁在 [`scripts/models.lock.json`](../../../../../../scripts/models.lock.json)。

## 怎么把模型弄到这里

```bash
python3 scripts/fetch_models.py            # 拉全部（缺什么拉什么，已就绪的只校验 sha256）
python3 scripts/fetch_models.py --check    # 只校验不下载
python3 scripts/fetch_models.py --help     # --only / --required-only / --force / --dest
```

只用标准库，不需要 uv / 虚拟环境。`scripts/build.sh` 打包前会自动跑一遍（`--strict`），
CI 也会（见 `.github/workflows/ci.yml`、`release.yml`），所以正常开发流程里你不用手动执行。

GitHub 直连拉不动时会自动换镜像（lock 的 `mirrors`，与 `scripts/manifest.json` 的
`download.sites` 同一批 gh-proxy 加速站）。内网 / 离线环境可以把源整个换掉：

```bash
MILOCO_MODELS_BASE_URL=https://mirror.example.com/miloco-models python3 scripts/fetch_models.py
```

## 清单

| 文件 | 必需 | 用途 |
| --- | --- | --- |
| `det_4C.onnx` | 是 | 人体/宠物检测 |
| `human_body_reid_v2.onnx` | 是 | 人体 ReID（跨帧同一人） |
| `bge-small-zh-v1.5-int8.onnx` | 否 | suggestion 事件链去重的句向量（缺则退回精确文本匹配，措辞一漂就认不出同一桩事、反复开新链） |
| `bge-small-zh-v1.5-tokenizer.json` | 否 | 上者的 tokenizer |
| `silero_vad.onnx` | 否 | `speeches` 字段的人声门控（缺则门控停用、退回纯能量 gate 行为） |

必需模型缺失时感知引擎会报 `models_missing`（见 `perception/engine/resource_validator.py`）；
可选模型缺失只降级对应能力，不阻塞启动。

## 终端用户不用管这些

release 安装包里已经带了模型（`miloco-models-{ver}.tar.gz`），`install.sh` 会解压到
`$MILOCO_HOME/models/`。

运行时路径是**两段式**解析，两段各自判各自的，别混着读：

- **第一段**由配置里的 `directories.models` 决定模型目录 —— 配了就用它（相对路径按
  `$MILOCO_HOME` 解析），**留空则取 `$MILOCO_HOME/models`**（见 `config/settings.py`
  的派生属性 `models_dir`）。注意这两种情况都**不是**本目录。生产链路上
  `perception/client.py` 总会把这一段的结果填进 `perception_model_dir`。
- **第二段**才是本目录，且只有**完全不传** `perception_model_dir` 时才会走到
  （`tracking_service._resolve_model_path` 与 `detector._DEFAULT_MODEL_PATH` 都从
  `__file__` 上溯到这儿）。

所以本目录主要是源码树里的开发 / 打包暂存位，但那条回退不是死代码 —— 直接构造感知引擎
且不填 `perception_model_dir` 的场景（测试、临时脚本）走的正是它，别当死代码删。注意装成
wheel 后本目录必然是空的（`pyproject.toml` 的 hatch exclude 不打 onnx），那种部署形态下
只能靠第一段指到 `$MILOCO_HOME/models`。

## 维护者：换模型怎么办

```bash
bash scripts/publish_models.sh upload <本地模型目录>   # 上传到 Release 并同步 lock
bash scripts/publish_models.sh refresh-lock            # 只按 Release 现状重算 lock
```

`models` 是固定 tag、资产可变，所以换完资产**必须**同步 lock，否则老 commit 锁的 sha256
对不上新资产，构建会失败。
