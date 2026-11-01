# Photo Vault iOS —— 日常命令都在这里，避免每次手敲一长串 xcodebuild。
#
#   make project    生成 PhotoVaultSync.xcodeproj（改 project.yml 后跑一次）
#   make build      编译（模拟器）
#   make test       跑单元测试
#   make run        编译 + 装进模拟器 + 启动
#   make open       用 Xcode 打开工程
#   make clean      清掉构建产物
#
# 换模拟器：make run SIM="iPhone 17 Pro Max"（默认 iPhone 17 Pro）
# 换后端：  App 内「设置 → 服务器地址」改成 http://<你的机器>:7878 即可。

SCHEME  ?= PhotoVaultSync
SIM     ?= iPhone 17 Pro
DEST    := platform=iOS Simulator,name=$(SIM)
DD      ?= .build   # 并行开发时每人用不同的：make build DD=.build/sync

.PHONY: all project build test run open clean icon

all: run

project:
	@which xcodegen >/dev/null || (echo "缺少 xcodegen：brew install xcodegen" && exit 1)
	xcodegen generate

$(DD)/.stamp:
	@mkdir -p $(DD)
	@touch $@

build: project
	xcodebuild -project PhotoVaultSync.xcodeproj -scheme $(SCHEME) \
		-destination "$(DEST)" -derivedDataPath $(DD) \
		-configuration Debug build | tail -30

test: project
	xcodebuild -project PhotoVaultSync.xcodeproj -scheme $(SCHEME) \
		-destination "$(DEST)" -derivedDataPath $(DD) \
		-configuration Debug test | tail -40

run: build
	xcrun simctl boot "$(SIM)" 2>/dev/null || true
	open -a Simulator
	xcrun simctl install booted "$(DD)/Build/Products/Debug-iphonesimulator/$(SCHEME).app"
	xcrun simctl launch booted com.photovault.ios

open: project
	open PhotoVaultSync.xcodeproj

icon:
	python3 tools/make_app_icon.py

clean:
	rm -rf $(DD) PhotoVaultSync.xcodeproj Support/Info.plist
