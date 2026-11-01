//
//  SyncView.swift
//  PhotoVaultSync
//
//  同步页：服务器状态 → 条件引导（未配置/离线/未授权/部分授权/有失败）→ 主同步卡
//  → 本机统计 → 同步范围 → 最近动态。
//  进度全部直接读 engine.state（@Observable），不自己轮询。
//

import SwiftUI
import UIKit

struct SyncView: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings
    @Environment(\.openURL) private var openURL

    private var engine: SyncEngine { model.engine }

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                header
                ServerStatusCard()
                guidanceCards
                SyncProgressCard()
                LocalLibraryCard()
                FilterSummaryCard()
                RecentActivityCard()
            }
            .vaultPagePadding()
            .padding(.top, 2)
        }
        .scrollIndicators(.hidden)
        .background(VaultBackground())
        .refreshable { await model.refreshAll() }
        .task { await model.bootstrap() }
    }

    // MARK: - 页头

    private var header: some View {
        PageHeader(
            eyebrow: "PHOTO VAULT",
            title: "同步",
            subtitle: headerSubtitle,
            subtitleColor: .secondary
        ) {
            if engine.state.isActive {
                StatusBadge(
                    text: phaseShortTitle,
                    color: Theme.phaseColor(engine.state.phase),
                    pulsing: true
                )
            } else if let last = settings.lastSyncAt ?? engine.stats.lastSyncAt {
                Text(TimeText.relative(last))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var headerSubtitle: String {
        switch engine.state.phase {
        case .scanning:
            return "正在扫描本机相册，统计需要上传的文件…"
        case .syncing:
            return "正在把本机相册传给家里的服务器"
        case .paused:
            return "已暂停，随时可以继续"
        case .failed:
            return "上次同步遇到问题，处理后可重试"
        case .finished:
            if let finishedAt = engine.state.finishedAt {
                return "上次同步完成于 \(TimeText.relative(finishedAt))"
            }
            return "上次同步已完成"
        case .idle:
            if let last = settings.lastSyncAt ?? engine.stats.lastSyncAt {
                return "上次同步 \(TimeText.relative(last))"
            }
            return "把 iPhone 相册备份到家里的 Photo Vault"
        }
    }

    private var phaseShortTitle: String {
        switch engine.state.phase {
        case .scanning: return "扫描中"
        case .syncing: return "同步中"
        case .paused: return "已暂停"
        case .finished: return "已完成"
        case .failed: return "有错误"
        case .idle: return "待开始"
        }
    }

    // MARK: - 条件引导（不要只做 happy path）

    @ViewBuilder
    private var guidanceCards: some View {
        if model.isReady && !model.hasServer {
            GuidanceCard(
                icon: "wifi.exclamationmark",
                tint: Theme.accent,
                title: "还没有连接服务器",
                message: "在设置里填入家里的 Photo Vault 地址（例如 http://bmc_desktop:7878）。连接测试通过后，就能同步相册和浏览服务器图库。",
                primaryTitle: "去设置",
                primaryAction: { model.goToSettings() }
            )
        } else if model.isServerOffline && !engine.state.isActive {
            GuidanceCard(
                icon: "bolt.horizontal.circle",
                tint: Theme.danger,
                title: "服务器连不上",
                message: model.serverErrorText ?? "确认家里的电脑开机、Docker 在跑，地址没变；也可能是本地网络权限没给。",
                primaryTitle: "重试连接",
                primaryAction: { Task { await model.testConnection() } }
            )
        }

        switch engine.authorizationStatus {
        case .authorized:
            EmptyView()
        case .limited:
            GuidanceCard(
                icon: "photo.badge.checkmark",
                tint: Theme.warning,
                title: "当前只能访问部分照片",
                message: "系统里你只允许了部分照片。想让相册里全部内容参与同步，可以重新选择 App 能访问的照片。",
                primaryTitle: "管理可访问照片",
                primaryAction: { Task { await engine.presentLimitedLibraryPicker() } }
            )
        case .notDetermined:
            GuidanceCard(
                icon: "photo.on.rectangle.angled",
                tint: Theme.accent,
                title: "需要访问相册的权限",
                message: "允许 App 读取相册后，才能扫描并上传你的照片和视频。只上传，不动本机相册。",
                primaryTitle: "允许访问相册",
                primaryAction: { Task { await engine.requestAuthorization() } }
            )
        case .denied, .restricted:
            GuidanceCard(
                icon: "lock.trianglebadge.exclamationmark",
                tint: Theme.danger,
                title: "相册访问已被拒绝",
                message: "系统不允许 App 读取相册。去「设置 → 隐私与安全性 → 照片」打开权限后再回来。",
                primaryTitle: "打开系统设置",
                primaryAction: { openSystemSettings() }
            )
        case .unknown:
            GuidanceCard(
                icon: "hourglass",
                tint: Color.gray,
                title: "正在确认相册权限",
                message: "如果这一步一直停着，可以去设置页重新检查权限状态。",
                primaryTitle: "去设置",
                primaryAction: { model.goToSettings() }
            )
        }

        if engine.stats.failed > 0 && !engine.state.isActive {
            GuidanceCard(
                icon: "exclamationmark.arrow.triangle.2.circlepath",
                tint: Theme.danger,
                title: "有 \(engine.stats.failed) 个文件没传成功",
                message: "失败通常来自网络中断或服务器临时问题。重试只会补传失败的，已成功的不会重复上传。",
                primaryTitle: "重试失败",
                primaryAction: { Task { await engine.retryFailed() } }
            )
        }
    }

    private func openSystemSettings() {
        guard let url = URL(string: UIApplication.openSettingsURLString) else { return }
        openURL(url)
    }
}

// MARK: - 服务器状态卡

private struct ServerStatusCard: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        VaultCard {
            VStack(spacing: 12) {
                HStack(spacing: 10) {
                    Image(systemName: "externaldrive.connected.to.line.below")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(healthColor)
                        .frame(width: 34, height: 34)
                        .background(Circle().fill(healthColor.opacity(0.13)))

                    VStack(alignment: .leading, spacing: 2) {
                        Text("服务器")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        // 这里只显示 host:port（+ 子路径），完整 URL 在设置页，避免被截断
                        Text(model.serverHostText)
                            .font(.system(.subheadline, design: .monospaced, weight: .medium))
                            .lineLimit(1)
                            .minimumScaleFactor(0.75)
                            .truncationMode(.middle)
                    }

                    Spacer(minLength: 8)
                    healthBadge
                }

                if let stats = model.remoteStats {
                    HStack(spacing: 0) {
                        StatTile(value: "\(model.serverLibraryCount)", label: model.serverLibraryLabel)
                        VaultDivider().frame(height: 26).padding(.horizontal, 10)
                        StatTile(value: "\(stats.count)", label: "服务器总资产")
                        VaultDivider().frame(height: 26).padding(.horizontal, 10)
                        StatTile(value: Format.bytes(stats.bytes), label: "总体积")
                    }
                }

                VaultDivider(vertical: false)

                HStack(alignment: .center, spacing: 10) {
                    Text(statusNote)
                        .font(.caption2)
                        .foregroundStyle(statusNoteColor)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 6)
                    Button {
                        Task { await model.testConnection() }
                    } label: {
                        if settings.isProbing || model.health == .checking {
                            ProgressView()
                                .controlSize(.mini)
                                .tint(Theme.accent)
                                .padding(.horizontal, 6)
                        } else {
                            Label("测试连接", systemImage: "antenna.radiowaves.left.and.right")
                        }
                    }
                    .buttonStyle(VaultMiniButtonStyle())
                    .accessibilityLabel("测试服务器连接")
                }
            }
        }
    }

    @ViewBuilder
    private var healthBadge: some View {
        switch model.health {
        case .online:
            StatusBadge(text: "在线", color: Theme.success)
        case .checking:
            StatusBadge(text: "连接中", color: Theme.warning, pulsing: true)
        case .offline:
            StatusBadge(text: "离线", color: Theme.danger)
        case .unknown:
            StatusBadge(text: "未检测", color: Color.gray)
        }
    }

    private var healthColor: Color {
        switch model.health {
        case .online: return Theme.success
        case .checking: return Theme.warning
        case .offline: return Theme.danger
        case .unknown: return Color.gray
        }
    }

    private var statusNote: String {
        switch model.health {
        case .online:
            let checked = model.lastCheckedAt.map { " · \(TimeText.relative($0))检测" } ?? ""
            return "目标库：\(settings.libraryTitle)\(checked)"
        case .checking:
            return "正在连接 \(model.serverAddressText)…"
        case .offline(let text):
            return text
        case .unknown:
            return "还没有检测过服务器连接"
        }
    }

    private var statusNoteColor: Color {
        switch model.health {
        case .offline: return Theme.danger.opacity(0.9)
        default: return .secondary
        }
    }
}

// MARK: - 主同步卡

private struct SyncProgressCard: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    private var engine: SyncEngine { model.engine }
    private var state: SyncState { engine.state }

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .top, spacing: 12) {
                    ZStack {
                        Circle()
                            .fill(Theme.phaseColor(state.phase).opacity(0.14))
                            .frame(width: 44, height: 44)
                        Image(systemName: Theme.phaseIcon(state.phase))
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(Theme.phaseColor(state.phase))
                    }

                    VStack(alignment: .leading, spacing: 3) {
                        Text(phaseTitle)
                            .font(.system(.headline, design: .rounded, weight: .semibold))
                        Text(phaseSubtitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Spacer(minLength: 4)

                    if state.isActive, let elapsed = state.elapsed {
                        Text(Format.duration(elapsed))
                            .font(.system(.caption, design: .monospaced, weight: .medium))
                            .foregroundStyle(Theme.phaseColor(state.phase))
                    }
                }

                VStack(spacing: 6) {
                    VaultProgressBar(fraction: barFraction, tint: Theme.phaseColor(state.phase))
                    HStack {
                        Text(progressCaption)
                            .font(.caption2)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer()
                        Text(percentText)
                            .font(.system(.caption2, design: .monospaced, weight: .semibold))
                            .foregroundStyle(Theme.phaseColor(state.phase))
                    }
                }

                HStack(spacing: 0) {
                    StatTile(value: "\(state.succeeded)", label: "已上传",
                             tint: state.succeeded > 0 ? Theme.success : .primary)
                    VaultDivider().frame(height: 26).padding(.horizontal, 10)
                    StatTile(value: "\(state.skipped)", label: "跳过")
                    VaultDivider().frame(height: 26).padding(.horizontal, 10)
                    StatTile(value: "\(state.failed)", label: "失败",
                             tint: state.failed > 0 ? Theme.danger : .primary)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("已上传 \(state.succeeded) 个，跳过 \(state.skipped) 个，失败 \(state.failed) 个")

                // 正在上传时：字节/速度/ETA 收进「正在上传 xxx」行右侧两行小字（更「在跑」）；
                // 暂停/完成等没有当前文件的状态才单独占一行。
                if state.isActive && !state.currentName.isEmpty {
                    currentFileRow
                } else if state.bytesSent > 0 {
                    transferRow
                }

                actionButtons
            }
        }
    }

    // MARK: 进度文案

    private var barFraction: Double? {
        if state.phase == .finished { return state.fraction ?? 1 }
        if state.isActive && state.fraction == nil { return nil }
        return state.fraction ?? 0
    }

    private var percentText: String {
        guard let fraction = barFraction else { return "…" }
        return "\(Int((fraction * 100).rounded()))%"
    }

    private var progressCaption: String {
        if let _ = state.fraction {
            return "已处理 \(state.completed) / \(state.total)"
        }
        switch state.phase {
        case .scanning:
            return "正在统计需要上传的文件…"
        case .finished:
            return "本轮完成"
        case .failed:
            return state.errorText ?? "同步中断"
        case .idle:
            return "等待开始"
        case .paused, .syncing:
            return "准备中…"
        }
    }

    private var phaseTitle: String {
        switch state.phase {
        case .idle: return "还没有开始"
        case .scanning: return "正在扫描相册"
        case .syncing: return "正在同步"
        case .paused: return "已暂停"
        case .finished: return "同步完成"
        case .failed: return "同步出错"
        }
    }

    private var phaseSubtitle: String {
        switch state.phase {
        case .idle:
            if !engine.authorizationStatus.isUsable {
                return "授权相册后就能把照片备份到家里的服务器"
            }
            if engine.stats.pending > 0 {
                return "本机还有 \(engine.stats.pending) 张待同步"
            }
            return "点下面的按钮开始扫描本机相册"
        case .scanning:
            return state.message.isEmpty ? "正在统计需要上传的文件…" : state.message
        case .syncing:
            if !state.message.isEmpty { return state.message }
            if let rate = state.bytesPerSecond {
                return "已发送 \(Format.bytes(state.bytesSent)) · \(Format.rate(rate))"
            }
            if !state.currentName.isEmpty { return "正在上传 \(state.currentName)" }
            return "正在上传…"
        case .paused:
            return "已暂停；在途的那一个会传完，点继续接着跑"
        case .finished:
            var parts: [String] = []
            parts.append("上传 \(state.succeeded)")
            if state.skipped > 0 { parts.append("跳过 \(state.skipped)") }
            if state.failed > 0 { parts.append("失败 \(state.failed)") }
            if let elapsed = state.elapsed { parts.append("耗时 \(Format.duration(elapsed))") }
            return parts.joined(separator: " · ")
        case .failed:
            return state.errorText ?? "遇到问题，检查服务器连接后可以重试"
        }
    }

    // MARK: 传输细节

    private var transferRow: some View {
        HStack(spacing: 14) {
            metaItem(icon: "arrow.up.circle", text: Format.bytes(state.bytesSent))
            if let rate = state.bytesPerSecond, state.isActive {
                metaItem(icon: "speedometer", text: Format.rate(rate))
            }
            if state.phase == .syncing, let eta = state.eta {
                metaItem(icon: "hourglass", text: "剩余 \(Format.duration(eta))")
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    private func metaItem(icon: String, text: String) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon).font(.system(size: 10, weight: .semibold))
            Text(text).font(.system(.caption2, design: .monospaced, weight: .medium))
        }
        .foregroundStyle(.secondary)
    }

    private var currentFileRow: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: "photo")
                .font(.system(size: 11))
                .foregroundStyle(Theme.accent)
            Text(state.currentName)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(Color.white.opacity(0.82))
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 6)
            transferStats
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.black.opacity(0.22)))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(Color.white.opacity(0.06)))
        .accessibilityLabel("正在上传 \(state.currentName)，已发送 \(Format.bytes(state.bytesSent))")
    }

    /// 当前文件行右侧两行小字：① 已传字节 · 速度 ② 剩余时间
    private var transferStats: some View {
        VStack(alignment: .trailing, spacing: 1) {
            HStack(spacing: 4) {
                Text(Format.bytes(state.bytesSent))
                if let rate = state.bytesPerSecond, state.isActive {
                    Text("·")
                    Text(Format.rate(rate))
                }
            }
            if state.phase == .syncing, let eta = state.eta {
                Text("剩余 \(Format.duration(eta))")
                    .foregroundStyle(Theme.accent.opacity(0.9))
            }
        }
        .font(.system(size: 10, weight: .medium, design: .monospaced))
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .accessibilityHidden(true)
    }

    // MARK: 按钮

    @ViewBuilder
    private var actionButtons: some View {
        VStack(spacing: 8) {
            HStack(spacing: 10) {
                if engine.canPause {
                    Button {
                        Haptics.tap()
                        engine.pause()
                    } label: {
                        Label("暂停", systemImage: "pause.fill")
                    }
                    .buttonStyle(VaultActionButtonStyle(variant: .primary, tint: Theme.warning))
                    .accessibilityLabel("暂停同步")

                    Button {
                        Haptics.warning()
                        Task { await engine.stop() }
                    } label: {
                        Label("停止", systemImage: "stop.fill")
                    }
                    .buttonStyle(VaultActionButtonStyle(variant: .danger))
                    .frame(width: 104)
                    .accessibilityLabel("停止同步")
                } else if engine.canResume {
                    Button {
                        Haptics.tap()
                        Task { await engine.resume() }
                    } label: {
                        Label("继续", systemImage: "play.fill")
                    }
                    .buttonStyle(VaultActionButtonStyle())
                    .accessibilityLabel("继续同步")

                    Button {
                        Haptics.warning()
                        Task { await engine.stop() }
                    } label: {
                        Label("停止", systemImage: "stop.fill")
                    }
                    .buttonStyle(VaultActionButtonStyle(variant: .danger))
                    .frame(width: 104)
                    .accessibilityLabel("停止同步")
                } else {
                    Button {
                        Haptics.tap()
                        Task { await startOrRetry() }
                    } label: {
                        Label(startButtonTitle, systemImage: startButtonIcon)
                    }
                    .buttonStyle(VaultActionButtonStyle())
                    .disabled(!engine.canStart)
                    .opacity(engine.canStart ? 1 : 0.45)
                    .accessibilityLabel(startButtonTitle)
                }
            }

            if let hint = startDisabledHint {
                Text(hint)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
            }
        }
    }

    private var startButtonTitle: String {
        switch state.phase {
        case .failed:
            return state.failed > 0 ? "重试失败" : "重新同步"
        case .finished:
            return "再同步一次"
        case .paused:
            return "继续同步"
        case .scanning, .syncing:
            return "同步中…"
        case .idle:
            return "开始同步"
        }
    }

    private var startButtonIcon: String {
        switch state.phase {
        case .failed: return "arrow.clockwise.circle.fill"
        case .finished: return "arrow.clockwise.circle.fill"
        case .idle: return "arrow.up.circle.fill"
        default: return "play.circle.fill"
        }
    }

    private var startDisabledHint: String? {
        if !engine.canStart && state.phase != .paused {
            if !engine.authorizationStatus.isUsable {
                switch engine.authorizationStatus {
                case .unknown: return "正在确认相册权限…"
                case .notDetermined: return "先允许 App 访问相册"
                case .denied, .restricted: return "相册权限被拒绝，去设置页打开"
                default: return "相册权限不可用"
                }
            }
            if !model.hasServer { return "先连接服务器，再开始同步" }
        }
        return nil
    }

    private func startOrRetry() async {
        if state.phase == .failed && state.failed > 0 {
            await engine.retryFailed()
        } else {
            await engine.start()
        }
    }
}

// MARK: - 本机统计卡

private struct LocalLibraryCard: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    private var stats: LocalLibraryStats { model.engine.stats }

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("本机相册")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer()
                    if stats.total > 0 {
                        Text("已同步 \(stats.synced) / \(stats.total)")
                            .font(.system(.caption2, design: .monospaced, weight: .medium))
                            .foregroundStyle(.secondary)
                    }
                }

                VaultProgressBar(fraction: stats.fraction,
                                 tint: stats.synced > 0 ? Theme.success : Theme.accent)

                HStack(spacing: 0) {
                    StatTile(value: "\(stats.total)", label: "总张数")
                    VaultDivider().frame(height: 26).padding(.horizontal, 10)
                    StatTile(value: "\(stats.synced)", label: "已同步", tint: Theme.success)
                    VaultDivider().frame(height: 26).padding(.horizontal, 10)
                    StatTile(value: "\(stats.pending)", label: "待同步", tint: Theme.accent)
                    if stats.failed > 0 {
                        VaultDivider().frame(height: 26).padding(.horizontal, 10)
                        StatTile(value: "\(stats.failed)", label: "失败", tint: Theme.danger)
                    }
                }
                .accessibilityElement(children: .combine)

                HStack(alignment: .center) {
                    Text(footnote)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    Spacer(minLength: 6)
                    Button {
                        Task { await model.engine.refreshLocalStats() }
                    } label: {
                        Label("刷新统计", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(VaultMiniButtonStyle())
                    .accessibilityLabel("刷新本机相册统计")
                }
            }
        }
    }

    private var footnote: String {
        if let last = stats.lastSyncAt ?? settings.lastSyncAt {
            var text = "上次成功同步 \(TimeText.relative(last))"
            if stats.bytesUploaded > 0 {
                text += " · 累计 \(Format.bytes(stats.bytesUploaded))"
            }
            return text
        }
        if stats.total == 0 && model.engine.authorizationStatus.isUsable {
            return "点「刷新统计」读取本机相册数量"
        }
        return "还没有同步记录"
    }
}

// MARK: - 同步范围摘要

private struct FilterSummaryCard: View {
    @Environment(AppModel.self) private var model
    @Environment(SettingsStore.self) private var settings

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("同步范围")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer()
                    Button {
                        model.goToSettings()
                    } label: {
                        Label("调整", systemImage: "slider.horizontal.3")
                    }
                    .buttonStyle(VaultMiniButtonStyle())
                    .accessibilityLabel("去设置调整同步范围")
                }

                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        VaultChip(icon: "photo.on.rectangle", text: settings.options.mediaSummary)
                        VaultChip(icon: "calendar", text: dateText)
                        VaultChip(icon: "externaldrive", text: "→ \(settings.libraryTitle)")
                        VaultChip(icon: "forward.fill",
                                  text: settings.options.skipExistingOnServer ? "跳过已有" : "全量上传")
                        VaultChip(icon: "square.stack.3d.up",
                                  text: "\(settings.options.concurrentUploads) 路并发")
                        if settings.options.includeHidden {
                            VaultChip(icon: "eye.slash", text: "含隐藏")
                        }
                    }
                    .padding(.vertical, 1)
                }
            }
        }
    }

    private var dateText: String {
        guard let date = settings.options.fromDate else { return "全部时间" }
        return "\(TimeText.day(date)) 起"
    }
}

// MARK: - 最近动态

private struct RecentActivityCard: View {
    @Environment(AppModel.self) private var model

    private var items: [SyncItem] {
        Array(model.engine.recent.suffix(30).reversed())
    }

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("最近动态")
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer()
                    if !model.engine.recent.isEmpty {
                        Text("\(model.engine.recent.count) 条")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }

                if items.isEmpty {
                    EmptyHint(
                        icon: "list.bullet.rectangle",
                        title: "还没有动态",
                        message: "开始同步后，这里会逐条显示每张照片的去向（成功 / 跳过 / 失败）。"
                    )
                } else {
                    LazyVStack(spacing: 7) {
                        ForEach(items) { item in
                            row(item)
                        }
                    }
                }
            }
        }
    }

    private func row(_ item: SyncItem) -> some View {
        HStack(alignment: .top, spacing: 9) {
            Image(systemName: statusIcon(item.state))
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(statusColor(item.state))
                .frame(width: 16, height: 16)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.system(.caption, design: .rounded))
                    .lineLimit(1)
                    .truncationMode(.middle)
                if item.state == .failed, let error = item.error, !error.isEmpty {
                    Text(error)
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.danger.opacity(0.9))
                        .lineLimit(2)
                }
            }

            Spacer(minLength: 6)

            VStack(alignment: .trailing, spacing: 2) {
                Text(statusText(item.state))
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(statusColor(item.state))
                Text(TimeText.clock(item.updatedAt))
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.tertiary)
            }
        }
    }

    private func statusIcon(_ state: SyncItemState) -> String {
        switch state {
        case .done: return "checkmark.circle.fill"
        case .skipped: return "arrow.uturn.forward.circle.fill"
        case .failed: return "xmark.circle.fill"
        case .uploading: return "arrow.up.circle.fill"
        case .pending: return "clock"
        }
    }

    private func statusColor(_ state: SyncItemState) -> Color {
        switch state {
        case .done: return Theme.success
        case .skipped: return .secondary
        case .failed: return Theme.danger
        case .uploading: return Theme.accent
        case .pending: return .secondary
        }
    }

    private func statusText(_ state: SyncItemState) -> String {
        switch state {
        case .done: return "已上传"
        case .skipped: return "跳过"
        case .failed: return "失败"
        case .uploading: return "上传中"
        case .pending: return "等待"
        }
    }
}

#Preview {
    SyncView()
        .environment(AppModel())
        .environment(SettingsStore())
        .preferredColorScheme(.dark)
}
