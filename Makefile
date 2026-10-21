# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
#
# 本地开发快捷命令。真正的构建逻辑仍在 scripts/build.sh 与 app/build_app.sh，
# 这里只做「一条命令搞定 + 打完成把产物路径打出来」，不复制它们的内部步骤。
#
#   make help             列出所有命令
#   make web              改完 web 源码后，重新构建并同步到 python 服务的静态目录
#   make slim-dmg         打包 slim 版 macOS 安装包（Miloco.app + dmg），打完输出安装包路径
#   make slim-app         只产 Miloco.app（跳过 dmg）
#
# 版本号一律用**当天日期** YYYY.M.D（月/日不补零，如 2026.10.21），不接收版本参数。
#
# 注意：macOS 自带的是 GNU Make 3.81，不支持 .ONESHELL / 二次展开等新语法，本文件只用 3.81 能跑的写法。

SHELL := /bin/bash

ROOT          := $(CURDIR)
WEB_DIR       := $(ROOT)/web
WEB_DIST      := $(WEB_DIR)/dist
STATIC_DIR    := $(ROOT)/backend/miloco/src/miloco/static
APP_DIR       := $(ROOT)/app
APP_OUT       := $(ROOT)/dist/app
APP_BUNDLE    := $(APP_OUT)/Miloco.app

# 前端产物条目：vite 的 outDir 是 web/dist，backend 的 static/ 才是真正被 python 服务
# 读的目录（见 settings.py directories.static_dir）。这里逐个列出来，sync 与校验共用。
WEB_ITEMS     := index.html assets fonts favicon.svg watch.html vendor
# 缺任何一个都说明这次构建是坏的：wheel 带上残缺 static 后，住户打开 1810 只会看到死页。
WEB_REQUIRED  := index.html assets fonts

# 版本号：固定用当天日期 YYYY.M.D（月/日不补零，与 CalVer 一致），不接收版本参数。
#   - 每次 make 执行时算一次（解析期），同一次构建里 web / App 用的是同一个版本号；
#   - 规范化交给 scripts/version_normalize.py（与发布流程同一套规则）。
VERSION       := $(shell date +%Y.%-m.%-d)

# 构建产物里带一个 .metadata_never_index，别让 Spotlight 索引 dist/ 下的 App 副本。
export COPYFILE_DISABLE = 1

.DEFAULT_GOAL := help
.PHONY: help resolve-version web web-install web-build web-sync web-clean slim-dmg slim-app slim-dmg-list

# ─── 帮助 ─────────────────────────────────────────────────────────────────────

help: ## 列出所有命令
	@awk 'BEGIN {FS = ":.*?## "; printf "\n用法：make <命令>\n\n命令：\n"} \
	     /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2} \
	     END {printf "\n"}' $(MAKEFILE_LIST)
	@printf "说明：\n"
	@printf "  版本号固定用当天日期（YYYY.M.D），不接受版本参数\n"
	@printf "  首次构建 web 会自动执行 pnpm install\n\n"

# ─── 版本号 ───────────────────────────────────────────────────────────────────

# 打印本次构建使用的版本号（PEP440 形式，即当天日期；各目标内部统一取这一份）。
resolve-version:
	@python3 $(ROOT)/scripts/version_normalize.py "$(VERSION)" --target pep440

# ─── 1. web 源码 → python 服务 ────────────────────────────────────────────────

web: ## 重新构建 web 并同步到 python 服务的静态目录（版本号 = 当天日期）
	@$(MAKE) --no-print-directory web-build
	@$(MAKE) --no-print-directory web-sync

web-install: ## 安装 web 依赖（按 pnpm-lock.yaml，无变化时很快）
	@cd $(WEB_DIR) && pnpm install --frozen-lockfile

web-build: ## 只构建 web（产物在 web/dist），不往 backend 里拷
	@if [ ! -d $(WEB_DIR)/node_modules ]; then \
		echo "==> web/node_modules 不存在，先安装依赖"; \
		$(MAKE) --no-print-directory web-install; \
	fi
	@echo "==> 构建 web（版本 $$($(MAKE) --no-print-directory resolve-version)）..."
	@cd $(WEB_DIR) && MILOCO_APP_VERSION="$$(cd $(ROOT) && $(MAKE) --no-print-directory resolve-version)" pnpm build
	@for item in $(WEB_REQUIRED); do \
		[ -e $(WEB_DIST)/$$item ] || { echo "!! web 构建产物缺失：web/dist/$$item" >&2; exit 1; }; \
	done
	@echo "    web/dist 就绪"

# 把 web/dist 里的运行产物同步进 backend static（会先清掉旧的 hash 文件名，
# 否则反复构建会累积一堆发不出去的旧 chunk）。assets/*.map 也删掉，
# 避免通过 /assets/*.map 直接拿到完整 TS 源码。
web-sync:
	@echo "==> 同步到 $(STATIC_DIR)"
	@mkdir -p $(STATIC_DIR)
	@rm -rf $(STATIC_DIR)/assets $(STATIC_DIR)/index.html
	@for item in $(WEB_ITEMS); do \
		if [ -e $(WEB_DIST)/$$item ]; then cp -R $(WEB_DIST)/$$item $(STATIC_DIR)/; fi; \
	done
	@rm -f $(STATIC_DIR)/assets/*.map
	@for item in $(WEB_REQUIRED); do \
		[ -e $(STATIC_DIR)/$$item ] || { echo "!! 同步后 static 仍缺 $$item" >&2; exit 1; }; \
	done
	@echo "    完成：刷新浏览器（或在跑 dev 时无需操作）即可看到最新页面"

web-clean: ## 清掉 web 构建产物与 python 服务里的静态副本
	@rm -rf $(WEB_DIST)
	@rm -rf $(STATIC_DIR)/assets $(STATIC_DIR)/index.html $(STATIC_DIR)/fonts \
		$(STATIC_DIR)/favicon.svg $(STATIC_DIR)/watch.html $(STATIC_DIR)/vendor
	@echo "已清理 web/dist 与 backend static"

# ─── 2. slim macOS 安装包 ─────────────────────────────────────────────────────

# 完整构建（含前端重建，所以网页的改动一定会带进 App）。
slim-dmg: ## 打包 slim 版 macOS 安装包（.app + .dmg），结束时输出安装包路径
	@command -v pnpm >/dev/null 2>&1 || { echo "!! 需要 pnpm（https://pnpm.io/installation）" >&2; exit 1; }
	@command -v uv   >/dev/null 2>&1 || { echo "!! 需要 uv（https://docs.astral.sh/uv/）" >&2; exit 1; }
	@mkdir -p $(APP_OUT)
	@touch $(ROOT)/dist/.metadata_never_index
	@echo "==> 构建 slim App 并打包 dmg（版本 $$($(MAKE) --no-print-directory resolve-version)，耗时数分钟）..."
	@bash $(APP_DIR)/build_app.sh --dmg --version "$$($(MAKE) --no-print-directory resolve-version)"
	@$(MAKE) --no-print-directory slim-dmg-list

# 前端没改、只想快速出一个 dmg 时用：跳过前端重建。
slim-app: ## 打包 slim 版 .app（跳过 dmg），适合装到 /Applications 直接用
	@bash $(APP_DIR)/build_app.sh --version "$$($(MAKE) --no-print-directory resolve-version)"
	@echo "App：$(APP_BUNDLE)"

# 找到本次（或最近一次）产出的 dmg，把绝对路径与大小打给住户/开发者。
slim-dmg-list:
	@dmg="$$(ls -t $(APP_OUT)/Miloco-*-arm64.dmg 2>/dev/null | head -1)"; \
	if [ -z "$$dmg" ]; then \
		echo "!! 没有找到 dmg，请先运行 make slim-dmg" >&2; exit 1; \
	fi; \
	echo ""; \
	echo "✅ 安装包：$$dmg"; \
	echo "   大小：  $$(du -h "$$dmg" | cut -f1)"; \
	echo "   打开：  open \"$$dmg\""
